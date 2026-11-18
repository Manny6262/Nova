"""Browser-level routing check. Chat requests are intercepted; no model/tool runs."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page()
    sent = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def intercept(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=200, content_type='application/x-ndjson', body='{"done":true}\n')

    page.route('**/chat', intercept)
    page.goto('http://127.0.0.1:5000/', wait_until='networkidle')
    permissions = page.evaluate('''() => ({
      full: fullControlEnabled, write: capWrite.checked, exec: capExec.checked,
      web: capWeb.checked, control: capControl.checked, screen: screenOn,
      approval: approvalMode, label: fullControlToggle.textContent
    })''')
    assert permissions == {'full': True, 'write': True, 'exec': True, 'web': True,
                           'control': True, 'screen': True, 'approval': 'bypass',
                           'label': 'FULL CONTROL · ON'}, permissions
    page.evaluate('fullControlToggle.click()')
    safe = page.evaluate('''() => ({full:fullControlEnabled, write:capWrite.checked,
      exec:capExec.checked, control:capControl.checked, approval:approvalMode})''')
    assert safe == {'full': False, 'write': False, 'exec': False,
                    'control': False, 'approval': 'ask'}, safe
    page.evaluate('fullControlToggle.click()')
    page.evaluate('''async () => { input.value='Synthetic full-control test'; await sendMessage(); }''')
    assert sent[-1]['caps'] == {'write': True, 'exec': True, 'web': True, 'control': True}
    assert sent[-1]['approval_mode'] == 'bypass'
    assert page.locator('#voiceAgentMode, #voiceEngine, #voicePause').count() == 0
    for enabled in (True, False):
        page.evaluate('''async enabled => {
          voiceActive=true;brainMode='codex';capControl.checked=enabled;ttsOn=false;
          input.value='Synthetic routing test';await sendMessage();voiceActive=false;
        }''', enabled)
        assert sent[-1]['provider'] == 'codex'
        assert sent[-1]['voice_version'] == 2
        assert sent[-1]['caps']['control'] is enabled
        assert sent[-1]['caps']['write'] is True
        assert sent[-1]['approval_mode'] == 'bypass'
    assert not errors, errors
    browser.close()
    print('PASS: voice v2 forwards selected provider and permissions; obsolete settings absent; no JS errors.')
