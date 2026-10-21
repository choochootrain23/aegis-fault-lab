"""Automated accessibility audit of the browser simulator with axe-core.

Serves the static site/ build and runs axe on the page in several states: first load with the
welcome dialog, the working page, an active investigation with open evidence, the shortcuts dialog,
the incident report and a phone-width layout. Fails on serious or critical violations.
axe-core is not vendored: download it once to state/ui-tools/axe/axe.min.js (see docs/VALIDATION.md)
or point AEGIS_AXE at a copy. Automated checks find only part of what matters; this is no
substitute for a screen-reader review.
Run from the project root with: python tests/accessibility_audit.py
"""
import functools
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
AXE = Path(os.getenv('AEGIS_AXE', ROOT / 'state' / 'ui-tools' / 'axe' / 'axe.min.js'))
BLOCKING = {'serious', 'critical'}


def audit(page, name, results):
    page.add_script_tag(path=str(AXE))
    found = page.evaluate("axe.run(document, {resultTypes: ['violations']}).then(r => r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.map(n => n.target.join(' ')).slice(0, 5)})))")
    results[name] = found
    for v in found:
        print(f"{name}: {v['impact']:>8} {v['id']} - {v['help']} ({len(v['nodes'])} shown: {v['nodes'][:2]})")


def main():
    if not AXE.exists():
        sys.exit(f'axe-core not found at {AXE}; see the module docstring')
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_preview.py')], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / 'site'))
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_address[1]}/'
    results = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.add_init_script("localStorage.setItem('aegis-tour-seen','1')")
            page.goto(url)
            expect(page.locator('#sim-welcome')).to_be_visible()
            audit(page, 'welcome dialog', results)
            page.click('#sim-continue')
            expect(page.locator('#start')).to_be_enabled(timeout=20000)
            audit(page, 'ready page', results)
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=20000)
            page.click('#node-catalog-b')
            for check in ('service', 'network', 'trace', 'config'):
                page.click(f'[data-check="{check}"]')
                page.wait_for_timeout(500)
            for summary in page.locator('.evidence-row summary').all()[:4]:
                summary.click()
            page.click('[data-help="action"]')
            page.locator('#ai-panel summary').click()
            audit(page, 'active investigation', results)
            page.keyboard.press('Escape')
            page.locator('body').click(position={'x': 5, 'y': 400})
            page.keyboard.press('?')
            expect(page.locator('#keys-dialog')).to_be_visible()
            audit(page, 'shortcuts dialog', results)
            page.click('#keys-close')
            page.click('#recover')
            expect(page.locator('#session-status')).to_have_text('recovered', timeout=20000)
            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=20000)
            expect(page.locator('#report .report-chart')).to_be_visible(timeout=10000)
            audit(page, 'incident report', results)
            phone = browser.new_page(viewport={'width': 390, 'height': 844})
            phone.add_init_script("localStorage.setItem('aegis-tour-seen','1')")
            phone.goto(url)
            phone.click('#sim-continue')
            expect(phone.locator('#start')).to_be_enabled(timeout=20000)
            audit(phone, 'phone layout', results)
            browser.close()
    finally:
        server.shutdown()
    output = ROOT / 'state' / 'ui-a11y'
    output.mkdir(parents=True, exist_ok=True)
    blocking = sorted({f"{state}: {v['id']}" for state, found in results.items() for v in found if v['impact'] in BLOCKING})
    (output / 'report.json').write_text(json.dumps({'passed': not blocking, 'blocking': blocking, 'results': results}, indent=2))
    if blocking:
        sys.exit('Serious or critical accessibility violations:\n  ' + '\n  '.join(blocking))
    print('Accessibility audit passed: no serious or critical axe violations. Report in state/ui-a11y/')


if __name__ == '__main__':
    main()
