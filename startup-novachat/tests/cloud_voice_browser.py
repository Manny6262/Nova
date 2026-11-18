"""Cloud voice routing and error presentation; no physical mic or computer actions."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page()
    sent = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    scenario = {'body': '{"error":"Cloud test: usage unavailable"}\n', 'status': 200}

    def chat(route):
        sent.append(route.request.post_data_json)
        route.fulfill(status=scenario['status'], content_type='application/x-ndjson',
                      body=scenario['body'])

    page.route('**/chat', chat)
    page.goto('http://127.0.0.1:5000/', wait_until='networkidle')

    def send():
        page.evaluate('''async () => {
            brainMode='codex'; voiceActive=true; ttsOn=false; screenOn=false;
            capControl.checked=false;
            capWrite.checked=false;capExec.checked=false;capWeb.checked=false;
            input.value='Synthetic cloud voice test';
            await sendMessage(); voiceActive=false;
        }''')

    send()
    assert sent[-1]['provider'] == 'codex' and sent[-1]['voice_version'] == 2
    assert not any(sent[-1]['caps'].values())
    assert 'Cloud test: usage unavailable' in page.locator('body').inner_text()
    assert 'Check that Ollama is running' not in page.locator('body').inner_text()
    print('PASS: conversation preserves cloud selection and actual stream error')

    scenario.update(status=400, body='{"error":"Request test: invalid configuration"}')
    send()
    assert 'Request test: invalid configuration' in page.locator('body').inner_text()
    print('PASS: HTTP JSON error is preserved')

    scenario.update(status=200, body='{"token":"Partial reply."}\n{"error":"Cloud test: interrupted"}\n')
    send()
    assert 'Partial reply.' in page.locator('body').inner_text()
    assert 'Cloud test: interrupted' in page.locator('body').inner_text()
    print('PASS: partial reply failure is visible')
    assert not errors, errors
    browser.close()
