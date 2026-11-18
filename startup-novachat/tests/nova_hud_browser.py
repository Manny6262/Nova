"""Original Nova appearance survives a saved legacy theme preference."""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport={'width': 1400, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("localStorage.setItem('novaTheme', 'jarvis')")
    page.goto('http://127.0.0.1:5000/', wait_until='networkidle')
    assert not page.locator('body.jarvis').count()
    assert not page.locator('#themeBtn, #themeSelect, link[href*=jarvis]').count()
    assert page.evaluate("localStorage.getItem('novaTheme')") is None
    assert page.locator('#brainSelect').is_visible()
    assert page.locator('#composer').is_visible()
    for width in (1400, 1024):
        page.set_viewport_size({'width': width, 'height': 900})
        for selector in ('#voiceModeBtn', '#capsBtn', '#brainSelect', '#send'):
            box = page.locator(selector).bounding_box()
            assert box and box['x'] >= 0 and box['x'] + box['width'] <= width, (width, selector, box)
    page.set_viewport_size({'width': 1400, 'height': 900})
    assert not errors, errors
    page.screenshot(path='tests/nova-hud-verified.png')
    browser.close()
    print('PASS: original Nova HUD, legacy preference retired, core controls visible, no JS errors')
