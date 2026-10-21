"""Optional browser-to-controller workflow against two real owned subprocesses.

The test adapter controls only those processes, not Docker. Collector cadence is
accelerated and a test-only route expires leases without a 15-minute wait.
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

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:8787'


def main():
    output = ROOT/'state'/'ui-workflow'
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 8787))
    errors = []
    with tempfile.TemporaryDirectory(prefix='aegis-workflow-') as temporary:
        with (output/'server.log').open('w') as log:
            env = {**os.environ, 'AEGIS_STATE': str(Path(temporary)/'workflow.sqlite'), 'AEGIS_BROWSER_TEST': '1'}
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'browser_harness:app',
                                        '--app-dir', 'tests', '--host', '127.0.0.1', '--port', '8787', '--no-access-log'],
                                       cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                for _ in range(150):
                    if process.poll() is not None:
                        raise RuntimeError('Test harness exited; inspect state/ui-workflow/server.log')
                    try:
                        with urlopen(BASE+'/api/state', timeout=.3) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Test harness did not become ready')
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(BASE)
                    expect(page.locator('#start')).to_be_enabled(timeout=15000)
                    page.locator('#start').click()
                    expect(page.locator('#session-status')).to_have_text('investigating', timeout=15000)
                    expect(page.locator('#node-gateway-a .node-state')).to_have_text('degraded')
                    expect(page.locator('#node-catalog-b .node-state')).to_have_text('unavailable')
                    page.screenshot(path=str(output/'investigating.png'), full_page=True)
                    first = page.locator('#evidence details').first
                    first.locator('summary').click()
                    expect(page.locator('#evidence-status')).to_contain_text('Reading paused')
                    evidence_before = page.locator('#evidence').inner_html()
                    page.wait_for_timeout(2300)
                    assert page.locator('#evidence').inner_html() == evidence_before, 'Polling moved an open observation'
                    page.locator('#evidence input').first.check()
                    page.locator('#explanation').fill('The gateway remains live while business requests fail; the catalogue is unavailable.')
                    page.locator('#uncertainty').fill('A failed request alone does not prove why the dependency stopped.')
                    page.locator('#save-note').click()
                    expect(page.locator('#saved-notes')).to_contain_text('gateway remains live')
                    page.locator('[data-help="evidence"]').click()
                    expect(page.locator('#help-result')).to_contain_text('business request fails')
                    expect(page.locator('#evidence .highlight').first).to_be_visible()
                    page.locator('#ai-panel summary').click()
                    expect(page.locator('#ai-badge')).to_contain_text('ON')
                    page.locator('#ai-question').fill('What happened?')
                    page.locator('#ai-ask').click()
                    expect(page.locator('#ai-answer')).to_contain_text('HARNESS FAKE')
                    expect(page.locator('#ai-answer')).to_contain_text('1 cited observations')
                    expect(page.locator('#evidence .highlight').first).to_be_visible()
                    page.locator('#node-catalog-b').click()
                    page.locator('[data-check="service"]').click()
                    expect(page.locator('#toast')).to_contain_text('Fresh observation')
                    # The accelerated collector pushes this check beyond its
                    # 60-row polling window while it remains valid guidance.
                    page.wait_for_timeout(4500)
                    page.locator('[data-help="action"]').click()
                    expect(page.locator('#help-result')).to_contain_text('catalog-b stopped')
                    expect(page.locator('#evidence .highlight')).to_contain_text('Inspection complete · stopped')
                    page.locator('#node-gateway-a').click()
                    page.locator('#recover').click()
                    expect(page.locator('#verification')).to_contain_text('has not restored', timeout=10000)
                    page.locator('#node-catalog-b').click()
                    page.locator('#recover').click()
                    expect(page.locator('#session-status')).to_have_text('recovered', timeout=15000)
                    page.locator('#reset').click()
                    expect(page.locator('#session-status')).to_have_text('ended', timeout=15000)
                    expect(page.locator('#notice')).to_contain_text('Investigation ended')
                    page.locator('#history [data-archive]').first.click()
                    expect(page.locator('#archive-notes')).to_contain_text('Verified recovery observations')
                    expect(page.locator('#archive-notes')).to_contain_text('Reset baseline observations')
                    page.locator('#archive-close').click()
                    page.locator('#mode').select_option('challenge')
                    expect(page.locator('#practice-target')).not_to_be_visible()
                    page.locator('#start').click()
                    expect(page.locator('#session-status')).to_have_text('investigating', timeout=15000)
                    expect(page.locator('#scenario-reveal')).to_be_empty()
                    # Lease manipulation exists only in the test harness.
                    page.evaluate("""async () => {
                      const s=await (await fetch('/api/state')).json();
                      const r=await fetch('/api/test/expire', {method:'POST', headers:{'X-Aegis-Token':s.token}});
                      if(!r.ok)throw Error('Test lease expiry failed');
                    }""")
                    expect(page.locator('#expiry-dialog')).to_be_visible(timeout=10000)
                    page.screenshot(path=str(output/'expiry.png'))
                    page.locator('#expiry-continue').click()
                    expect(page.locator('#expiry-dialog')).not_to_be_visible()
                    expect(page.locator('#timer')).to_contain_text('remaining')
                    # Close the browser: expiry recovery must continue in the controller.
                    page.evaluate("""async () => {
                      const s=await (await fetch('/api/state')).json();
                      const r=await fetch('/api/test/expire?cleanup=true', {method:'POST', headers:{'X-Aegis-Token':s.token}});
                      if(!r.ok)throw Error('Test lease expiry failed');
                    }""")
                    browser.close()
                for _ in range(100):
                    with urlopen(BASE+'/api/state', timeout=2) as response:
                        state = json.load(response)
                    if state['session']['status'] == 'ended':
                        break
                    time.sleep(.1)
                assert state['session']['status'] == 'ended'
                assert state['session']['closure'] == 'automatic_reset'
                assert state['session']['outcome'] == 'reset'
                assert not errors, errors
                (output/'report.json').write_text(json.dumps({
                    'passed': True, 'page_errors': errors, 'final_session': state['session'],
                    'scope': 'Real browser/controller/service HTTP; test-only owned subprocess control and accelerated lease/cadence. Not Docker validation.'
                }, indent=2)+'\n')
                print('Browser workflow passed. Artifacts: '+str(output))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                mutations = Path(temporary)/'process-mutations.json'
                if mutations.exists():
                    (output/'process-mutations.json').write_text(mutations.read_text())


if __name__ == '__main__':
    main()
