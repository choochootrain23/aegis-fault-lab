"""Optional real-browser review using an isolated database with labelled test records.

Requires Playwright and its Chromium browser. Never injects a Docker fault.
Run from the project root with: python tests/browser_review.py
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aegis.store import Store
from playwright.sync_api import sync_playwright, expect


def main():
    output = ROOT/'state'/'ui-review'
    output.mkdir(parents=True, exist_ok=True)
    # Refuse to reuse or terminate someone else's server on the application's port.
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 8787))
    errors = []
    with tempfile.TemporaryDirectory(prefix='aegis-ui-') as temporary:
        database = Path(temporary)/'review.sqlite'
        store = Store(database)
        observation = store.record('browser-review', 'catalog-b', 'service',
                                   {'ok': True, 'running': False, 'source': 'browser test fixture, not a live measurement'})
        store.save({'id': 'browser-review', 'mode': 'practice', 'status': 'ended',
                    'started_at': time.time()-120, 'ended_at': time.time(),
                    'fault_kind': 'service-unavailable', 'fault_target': 'catalog-b',
                    'outcome': 'reset', 'revealed': True, 'verification': 'Browser test fixture',
                    'notes': [{'at': time.time(), 'explanation': 'Browser test note <img src=x onerror=alert(1)>',
                               'uncertainty': 'These are seeded UI test records.', 'evidence_ids': [observation['id'], 'expired-fixture']}],
                    'help': [{'at': time.time(), 'level': 'evidence', 'message': 'Browser test assistance',
                              'evidence_ids': [observation['id']]}],
                    'recovery_attempts': [{'key': 'browser-test-action', 'at': time.time(), 'node': 'catalog-b', 'status': 'unknown',
                                           'detail': 'Browser test action; no Docker mutation occurred.'}]})
        store.db.close()
        with (output/'server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'aegis.controller:app',
                                        '--host', '127.0.0.1', '--port', '8787', '--no-access-log'],
                                       cwd=ROOT, env={**os.environ, 'AEGIS_STATE': str(database)},
                                       stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError('Review server failed; inspect state/ui-review/server.log')
                    try:
                        with urlopen('http://127.0.0.1:8787/api/state', timeout=.3) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(.05)
                else:
                    raise RuntimeError('Review server did not become ready')
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto('http://127.0.0.1:8787')
                    expect(page.locator('#history [data-archive]')).to_have_count(1)
                    expect(page.locator('#start')).to_be_disabled()
                    expect(page.locator('#recover')).to_be_disabled()
                    expect(page.locator('#suggested-check')).not_to_be_visible()
                    expect(page.locator('#evidence')).not_to_contain_text(observation['id'])
                    history_button = page.locator('#history [data-archive]')
                    history_button.focus()
                    page.wait_for_timeout(2300)  # Cross a real polling refresh.
                    expect(history_button).to_be_focused()
                    page.screenshot(path=str(output/'desktop.png'), full_page=True)
                    page.locator('#history [data-archive]').click()
                    expect(page.locator('#archive-dialog')).to_be_visible()
                    expect(page.locator('#archive-warning')).to_contain_text('1 cited observations')
                    expect(page.locator('#archive-notes')).to_contain_text('Browser test assistance')
                    expect(page.locator('#archive-notes img')).to_have_count(0)
                    page.locator('[data-citation="'+observation['id']+'"]').first.click()
                    expect(page.locator('#archive-citation')).to_contain_text('not a live measurement')
                    expect(page.locator('#archive-citation details')).to_have_attribute('open', '')
                    page.screenshot(path=str(output/'archive.png'))
                    page.locator('[data-citation="expired-fixture"]').click()
                    expect(page.locator('#toast')).to_contain_text('Observation expired')
                    page.keyboard.press('Escape')
                    expect(page.locator('#archive-dialog')).not_to_be_visible()
                    expect(history_button).to_be_focused()
                    page.locator('#toast').evaluate('(el)=>el.hidden=true')
                    for width in (390, 320):
                        page.set_viewport_size({'width': width, 'height': 844})
                        page.evaluate('window.scrollTo(0,0)')
                        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), f'Page overflows at {width}px'
                        page.screenshot(path=str(output/f'mobile-{width}.png'), full_page=True)
                        page.locator('.fleet-panel').screenshot(path=str(output/f'topology-{width}.png'))
                    page.locator('#history [data-archive]').click()
                    expect(page.locator('#archive-dialog')).to_be_visible()
                    assert page.locator('#archive-dialog').evaluate('(el)=>el.scrollWidth<=el.clientWidth'), 'Archive overflows on mobile'
                    page.screenshot(path=str(output/'mobile-archive.png'))
                    page.locator('#archive-close').click()
                    # Leave polling unanswered: the UI must time out and stop
                    # presenting its last successful connection as current.
                    pending = []
                    page.route('**/api/state', lambda route: pending.append(route))
                    expect(page.locator('#notice')).to_contain_text('response timed out', timeout=12000)
                    expect(page.locator('[data-check="liveness"]')).to_be_disabled()
                    expect(page.locator('#maintenance')).to_be_disabled()
                    expect(page.locator('#node-gateway-a .node-state')).to_have_text('Unknown')
                    for route in pending:
                        route.abort()
                    page.unroute('**/api/state')
                    expect(page.locator('[data-check="liveness"]')).to_be_enabled(timeout=10000)
                    browser.close()
                assert not errors, errors
                (output/'report.json').write_text(json.dumps({
                    'passed': True, 'viewports': [1440, 390, 320], 'page_errors': errors,
                    'scope': 'Real Chromium, real controller archive routes, isolated labelled test database; no live Docker validation.'
                }, indent=2)+'\n')
                print('Browser review passed. Artifacts: '+str(output))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
