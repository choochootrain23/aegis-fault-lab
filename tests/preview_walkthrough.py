"""Headless Chromium walkthrough of the static browser-simulator preview.

Builds site/, serves it on a temporary loopback port and drives a practice and a
challenge investigation. This validates the simulator only, never Docker or the
real controller. Requires Playwright and Chromium (see docs/VALIDATION.md).
Run from the project root with: python tests/preview_walkthrough.py
"""
import functools
import http.server
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
from playwright.sync_api import sync_playwright, expect


def main():
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_preview.py')], check=True)
    output = ROOT / 'state' / 'ui-preview'
    output.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / 'site'))
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_address[1]}/'
    errors = []
    try:
        with sync_playwright() as p:
            browser = getattr(p, os.getenv('AEGIS_BROWSER', 'chromium')).launch()
            page = browser.new_page(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.locator('#sim-welcome')).to_be_visible()
            expect(page.locator('#sim-welcome')).to_contain_text('not the real lab')
            page.screenshot(path=str(output / 'welcome.png'))
            page.click('#sim-continue')
            expect(page.locator('#sim-welcome')).to_be_hidden()
            # First visit: the tour starts, and every step finds its target.
            expect(page.locator('.tour-card')).to_be_visible()
            for step in range(1, 9):
                expect(page.locator('#tour-count')).to_have_text(f'Tour · {step} of 8')
                page.click('#tour-next')
            expect(page.locator('.tour-card')).to_be_hidden()
            # The tour stays one click away in the sticky bar.
            page.evaluate('scrollTo(0, 2000)')
            expect(page.locator('#tour-start')).to_be_in_viewport()
            page.click('#tour-start')
            expect(page.locator('#tour-count')).to_have_text('Tour · 1 of 8')
            page.click('#tour-skip')
            expect(page.locator('.tour-card')).to_be_hidden()
            page.evaluate('scrollTo(0, 0)')
            expect(page.locator('.sim-strip')).to_contain_text('SIMULATION')
            expect(page.locator('.sim-stamp')).to_be_visible()
            expect(page.locator('.sim-banner')).to_have_count(0)
            expect(page.locator('.sim-strip #sim-skip')).to_be_attached()
            page.evaluate("document.getElementById('evidence-section').scrollIntoView({block:'start'})")
            expect(page.locator('.nav-links a.nav-active')).to_have_text('Evidence')
            for link in ('Diagnosis', 'History', 'Evidence'):
                page.click(f'.nav-links a:text-is("{link}")')
                expect(page.locator('.nav-links a.nav-active')).to_have_text(link)
            page.wait_for_timeout(1000)
            page.evaluate('scrollTo(0, 0)')
            expect(page.locator('.nav-links a.nav-active')).to_have_text('Fleet')
            expect(page.locator('footer')).not_to_contain_text('Real processes')
            expect(page.locator('#ai-badge')).to_have_text('OFF')
            expect(page.locator('#ai-status')).to_contain_text('Not available in the browser simulator')
            # Slower browsers may already have finished the ten-second baseline by now.
            expect(page.locator('#next-title')).to_have_text(re.compile('Collecting a healthy baseline|Ready when you are'))
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            expect(page.locator('#next-title')).to_have_text('Ready when you are')

            # Live map: drag moves a node and its edge; a drag is not a click; layout persists and resets.
            catalog = page.locator('#node-catalog-b')
            page.evaluate("document.getElementById('topology').scrollIntoView({block:'center'})")
            edge_before = page.locator('#edge').get_attribute('d')
            box = catalog.bounding_box()
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            page.mouse.down()
            page.mouse.move(box['x'] + box['width'] / 2 - 60, box['y'] + box['height'] / 2 + 70, steps=8)
            page.mouse.up()
            moved = catalog.bounding_box()
            # Layouts are stored as fractions of the map, so compare node centres as fractions too.
            in_map = lambda: page.evaluate("(() => { const n = document.getElementById('node-catalog-b').getBoundingClientRect(), m = document.getElementById('topology').getBoundingClientRect(); return [(n.x + n.width / 2 - m.x) / m.width, (n.y + n.height / 2 - m.y) / m.height]; })()")
            moved_in_map = in_map()
            assert abs(moved['y'] - box['y']) > 40, 'drag did not move the node'
            assert page.locator('#edge').get_attribute('d') != edge_before, 'edge did not follow the node'
            expect(page.locator('#inspector-title')).to_have_text('gateway-a')
            catalog.click()
            expect(page.locator('#inspector-title')).to_have_text('catalog-b')
            page.locator('[data-graph-node="broker"]').focus()
            broker_before = page.locator('[data-graph-node="broker"]').bounding_box()
            page.keyboard.press('Shift+ArrowLeft')
            assert page.locator('[data-graph-node="broker"]').bounding_box()['x'] < broker_before['x'] - 5
            expect(page.locator('#edge-control-catalog-b')).to_have_attribute('d', re.compile(r'^M'))
            page.reload()
            page.click('#sim-continue')
            expect(page.locator('.tour-card')).to_be_hidden()
            page.evaluate("document.getElementById('topology').scrollIntoView({block:'center'})")
            page.wait_for_timeout(300)
            after_reload = in_map()
            assert all(abs(a - b) < 0.01 for a, b in zip(after_reload, moved_in_map)), f'layout was not remembered: {moved_in_map} -> {after_reload}'
            page.click('#topology-plane')
            expect(page.locator('[data-graph-node="controller"]')).to_be_hidden()
            page.click('#topology-plane')
            page.click('#topology-reset')
            assert abs(in_map()[1] - moved_in_map[1]) > 0.08, 'reset did not restore the layout'
            page.click('#node-gateway-a')
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            page.screenshot(path=str(output / 'map.png'))
            expect(page.locator('.packets.dependency').first).to_have_class(re.compile(r'\bflow\b'))
            page.keyboard.press('?')
            expect(page.locator('#keys-dialog')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#keys-dialog')).to_be_hidden()
            page.keyboard.press('b')
            expect(page.locator('#inspector-title')).to_have_text('catalog-b')
            page.keyboard.press('c')
            expect(page.locator('#inspector-title')).to_have_text('gateway-c')
            page.keyboard.press('a')
            expect(page.locator('#inspector-title')).to_have_text('gateway-a')
            expect(page.locator('#steps li.current')).to_have_text('02Start')
            expect(page.locator('#node-gateway-a .node-state')).to_have_text('healthy')

            # Practice: shared dependency failure, wrong-node attempt, verified recovery.
            page.select_option('#practice-target', 'catalog-b')
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=15000)
            expect(page.locator('#node-gateway-a .node-state')).to_have_text('degraded')
            expect(page.locator('#node-catalog-b .node-state')).to_have_text('unavailable')
            expect(page.locator('#node-gateway-c .node-state')).to_have_text('degraded')
            expect(page.locator('.packets.dependency.blocked')).to_have_count(2)
            page.click('[data-check="liveness"]')
            expect(page.locator('#toast')).to_contain_text('Fresh observation')
            page.click('[data-check="business"]')
            expect(page.locator('#toast')).to_contain_text('Fresh observation')
            page.click('#node-catalog-b')
            page.click('[data-check="service"]')
            expect(page.locator('.evidence-row').filter(has_text='stopped').first).to_be_visible()
            expect(page.locator('[data-check="service"] .check-result')).to_contain_text('stopped')
            expect(page.locator('#next-title')).to_have_text('Find the boundary of the failure')
            expect(page.locator('#edge')).to_have_class(re.compile(r'\bfailed\b'))
            expect(page.locator('.packets.dependency').first).to_have_class(re.compile(r'\bblocked\b'))
            page.screenshot(path=str(output / 'investigating.png'))
            page.click('[data-help="action"]')
            expect(page.locator('#help-result')).to_contain_text('catalog-b stopped')
            expect(page.locator('.evidence-row.highlight')).to_have_count(1)
            page.click('#node-gateway-a')
            page.click('#recover')
            expect(page.locator('#action-history')).to_contain_text('Already running; nothing changed', timeout=5000)
            page.wait_for_timeout(7000)
            expect(page.locator('#session-status')).not_to_have_text('recovered')
            page.click('#node-catalog-b')
            page.click('#recover')
            expect(page.locator('#session-status')).to_have_text('recovered', timeout=15000)
            expect(page.locator('#next-title')).to_have_text('Recovery verified')
            expect(page.locator('#celebrate .celebrate-banner')).to_be_visible()
            expect(page.locator('#edge')).to_have_class(re.compile(r'\bflowing\b'))
            expect(page.locator('#node-gateway-a .node-state')).to_have_text('healthy')
            expect(page.locator('#cited-list')).to_contain_text('No evidence cited yet')
            page.locator('.evidence-row input').first.check()
            expect(page.locator('#selected-count')).to_have_text('1 cited')
            expect(page.locator('#cited-list .cited-chip')).to_have_count(1)
            page.fill('#explanation', 'Gateway stayed live while business requests failed; catalogue container was stopped.')
            page.click('#save-note')
            expect(page.locator('#saved-notes')).to_contain_text('1 cited observations')
            with page.expect_download() as download:
                page.click('#export')
            exported = json.loads(Path(download.value.path()).read_text())
            assert 'SIMULATED' in exported['export_note'] and exported['evidence'], exported['export_note']
            assert all(e['body'].get('simulated') for e in exported['evidence'])
            page.screenshot(path=str(output / 'recovered.png'), full_page=True)
            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=15000)
            # Incident report: separate measures, two latency series and a working replay.
            expect(page.locator('#report .report-tile')).to_have_count(6)
            expect(page.locator('#report .report-chart path.series')).to_have_count(3)
            expect(page.locator('#report .report-chart path.failure').first).to_be_attached()
            page.locator('#report-section').scroll_into_view_if_needed()
            page.click('#report .replay-play')
            page.wait_for_timeout(1500)
            page.click('#report .replay-play')
            expect(page.locator('#report .replay-readout')).to_contain_text('gateway-a')
            page.screenshot(path=str(output / 'report.png'), full_page=False)
            page.click('[data-archive]')
            expect(page.locator('#archive-title')).to_contain_text('practice investigation')
            page.locator('#archive-notes [data-citation]').first.click()
            expect(page.locator('#archive-citation')).to_be_visible()
            expect(page.locator('#archive-report .report-chart')).to_be_visible()
            page.click('#archive-close')

            # CPU starvation on the catalogue: slow, not down; resources reveal it; stopping the job recovers.
            page.select_option('#practice-kind', 'cpu-starvation')
            page.select_option('#practice-target', 'catalog-b')
            expect(page.locator('#scenario-title')).to_have_text('When a service is starved of CPU')
            expect(page.locator('#scenario-number')).to_have_text('02')
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=20000)
            page.click('#node-catalog-b')
            page.click('[data-check="resources"]')
            expect(page.locator('[data-check="resources"] .check-result')).to_contain_text(re.compile(r'CPU (9\d(\.\d)?|100)%'))
            page.click('[data-help="action"]')
            expect(page.locator('#help-result')).to_contain_text('runaway job')
            page.click('[data-remedy="calm"]')
            expect(page.locator('#session-status')).to_have_text('recovered', timeout=20000)
            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=15000)

            # Disk full on the gateway: a local fault; disk usage reveals it; clearing recovers.
            page.select_option('#practice-kind', 'disk-full')
            page.select_option('#practice-target', 'gateway-a')
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=20000)
            expect(page.locator('#node-catalog-b .node-state')).to_have_text('healthy')
            page.click('#node-gateway-a')
            page.click('[data-check="disk"]')
            expect(page.locator('[data-check="disk"] .check-result')).to_contain_text('100% used')
            page.click('[data-remedy="calm"]')
            expect(page.locator('#toast')).to_contain_text('Nothing changed')
            page.click('[data-remedy="clear"]')
            expect(page.locator('#session-status')).to_have_text('recovered', timeout=20000)
            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=15000)

            # Config drift on the gateway: looks like a dependency failure until the catalogue and config are read.
            page.select_option('#practice-kind', 'config-drift')
            page.select_option('#practice-target', 'gateway-a')
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=20000)
            expect(page.locator('#node-catalog-b .node-state')).to_have_text('healthy')
            page.click('#node-gateway-a')
            page.keyboard.press('6')
            expect(page.locator('[data-check="config"] .check-result')).to_contain_text('drifted')
            page.keyboard.press('7')
            expect(page.locator('[data-check="trace"] .check-result')).to_contain_text('failed')
            trace = page.locator('.evidence-row').filter(has_text='Trace · failed').first
            trace.locator('summary').click()
            expect(trace.locator('.waterfall')).to_contain_text('ConnectError')
            config = page.locator('.evidence-row').filter(has_text='Config drifted').first
            config.locator('summary').click()
            expect(config.locator('.config-table tr.drifted')).to_contain_text(':8080')
            page.screenshot(path=str(output / 'config-drift.png'))
            page.click('[data-help="evidence"]')
            expect(page.locator('#help-result')).to_contain_text('running config')
            page.click('[data-help="action"]')
            expect(page.locator('#help-result')).to_contain_text('restoring the deployed config')
            page.click('[data-remedy="restore"]')
            expect(page.locator('#session-status')).to_have_text('recovered', timeout=20000)
            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=15000)

            # Network delay on the catalogue: slow, CPU idle; the network path shows the netem queue.
            page.select_option('#practice-kind', 'network-delay')
            page.select_option('#practice-target', 'catalog-b')
            expect(page.locator('#scenario-title')).to_have_text('When the network lags')
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=20000)
            page.keyboard.press('b')
            page.keyboard.press('8')
            expect(page.locator('[data-check="network"] .check-result')).to_contain_text('connect 6')
            row = page.locator('.evidence-row').filter(has_text='netem queue').first
            row.locator('summary').click()
            expect(row.locator('.config-table tr.drifted')).to_contain_text('600 ms')
            page.click('[data-help="action"]')
            expect(page.locator('#help-result')).to_contain_text('removing the traffic delay')
            page.click('[data-remedy="undelay"]')
            expect(page.locator('#session-status')).to_have_text('recovered', timeout=25000)
            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=15000)

            # A stopped gateway-c is local: gateway-a and the catalogue stay healthy, and guidance says so.
            page.select_option('#practice-kind', 'service-unavailable')
            page.select_option('#practice-target', 'gateway-c')
            expect(page.locator('#scenario-eyebrow')).to_contain_text('gateway-c')
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=20000)
            expect(page.locator('#node-gateway-c .node-state')).to_have_text('unavailable')
            expect(page.locator('#node-gateway-a .node-state')).to_have_text('healthy')
            page.click('[data-help="evidence"]')
            expect(page.locator('#help-result')).to_contain_text('local to gateway-c')
            page.click('#node-gateway-c')
            page.click('#recover')
            expect(page.locator('#session-status')).to_have_text('recovered', timeout=20000)
            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=15000)

            # Challenge: hidden target, timer skip and expiry continuation.
            page.select_option('#mode', 'challenge')
            expect(page.locator('#scenario-number')).to_have_text('??')
            expect(page.locator('#scenario-title')).to_have_text('A hidden fault')
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=15000)
            state = page.evaluate("fetch('api/state').then(r=>r.json())")
            assert 'scenario' not in state['session'] and 'fault_target' not in state['session']
            heading = page.locator('.scenario-strip').inner_text()
            assert 'gateway-a' not in heading and 'catalog-b' not in heading, 'the heading must not name the hidden target'
            journey = page.locator('#journey').inner_text()
            assert 'gateway-a' not in journey and 'catalog-b' not in journey, 'directions must not name the hidden target'
            page.click('#sim-skip')
            expect(page.locator('#expiry-dialog')).to_be_visible(timeout=50000)
            expect(page.locator('#grace-text')).to_contain_text('Automatic reset')
            page.click('#expiry-continue')
            expect(page.locator('#expiry-dialog')).to_be_hidden()
            expect(page.locator('#timer')).to_contain_text('remaining')
            page.click('#reveal')
            expect(page.locator('#scenario-reveal')).to_contain_text('Known scenario:')
            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=15000)
            expect(page.locator('#history .history-item')).to_have_count(7)

            mobile = browser.new_page(viewport={'width': 390, 'height': 844})
            mobile.on('pageerror', lambda error: errors.append(str(error)))
            mobile.goto(url)
            expect(mobile.locator('#sim-welcome')).to_be_visible()
            mobile.screenshot(path=str(output / 'welcome-mobile.png'))
            mobile.click('#sim-continue')
            expect(mobile.locator('#start')).to_be_enabled(timeout=20000)
            overflow = mobile.evaluate('document.documentElement.scrollWidth - innerWidth')
            mobile.screenshot(path=str(output / 'mobile-390.png'), full_page=True)
            assert overflow <= 0, f'horizontal overflow {overflow}px'
            browser.close()
    finally:
        server.shutdown()
    report = {'passed': not errors, 'page_errors': errors, 'url': url}
    (output / 'report.json').write_text(json.dumps(report, indent=2))
    if errors:
        sys.exit(f'Page errors: {errors}')
    print('Preview walkthrough passed; screenshots in state/ui-preview/')


if __name__ == '__main__':
    main()
