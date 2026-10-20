"""Opt-in browser challenge/restart walkthrough against the real Docker project."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from playwright.sync_api import sync_playwright, expect
from validate_f1 import Operator, BASE
from validate_scenarios import anomalies, REMEDY

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    if not parser.parse_args().run:
        parser.error('--run is required; this interrupts project services and restarts the controller')
    operator = Operator()
    initial = operator.request('/api/state')
    if initial['session'] and initial['session']['status'] != 'ended':
        parser.error('Finish the active investigation first')
    output = ROOT/'state'/'validation'/('browser-'+uuid.uuid4().hex)
    output.mkdir(parents=True)
    sid = None
    report = {'passed': False, 'scope': 'Production Docker/browser challenge and controller restart; no test adapter'}
    errors = []
    try:
        operator.wait(lambda s:s['baseline_cycles'] >= 5, 'healthy baseline')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(BASE)
                expect(page.locator('#start')).to_be_enabled(timeout=15000)
                page.locator('#mode').select_option('challenge')
                with page.expect_response('**/api/sessions') as started:
                    page.locator('#start').click()
                response = started.value
                assert response.ok, 'Challenge admission failed'
                sid = response.json()['id']
                report['session_id'] = sid
                expect(page.locator('#session-status')).to_have_text('investigating', timeout=20000)
                expect(page.locator('#scenario-reveal')).to_be_empty()
                for public in (operator.request('/api/state')['session'],
                               operator.request('/api/export?session_id='+sid)['session']):
                    assert not any(key in public for key in ('scenario', 'fault_target', 'fault_kind', 'fault_action', 'debrief'))
                page.screenshot(path=str(output/'challenge.png'), full_page=True)
                before = operator.request('/api/state')['session']
                subprocess.run(['docker', 'compose', 'restart', 'controller'], cwd=ROOT,
                               check=True, capture_output=True, timeout=30)
                deadline = time.monotonic()+30
                while time.monotonic() < deadline:
                    try:
                        current = operator.request('/api/state')['session']
                        break
                    except RuntimeError:
                        time.sleep(.5)
                else:
                    raise RuntimeError('Controller did not return after restart')
                assert current['id'] == sid
                assert current['recovery_attempts'] == before['recovery_attempts']
                operator.wait(lambda s:all(n['health'] != 'unknown' for n in s['nodes']), 'fresh restart measurements', sid=sid)
                # Determine the fault from fresh permitted inspections, never from the fault ledger.
                found, _ = anomalies(operator)
                assert len(found) == 1, found
                kind, target, _ = found[0]
                remedy = page.locator(f'[data-remedy="{REMEDY[kind]}"]')
                expect(remedy).to_be_enabled(timeout=15000)
                page.locator('#node-'+target).click()
                remedy.click()
                expect(page.locator('#session-status')).to_have_text('recovered', timeout=25000)
                page.locator('#reset').click()
                expect(page.locator('#session-status')).to_have_text('ended', timeout=25000)
                page.set_viewport_size({'width':390, 'height':844})
                page.screenshot(path=str(output/'mobile-restored.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                assert not errors, errors
                report.update(passed=True, page_errors=errors, restart_preserved_session=True,
                              recovered_node=target, fault_kind=kind)
            finally:
                browser.close()
    except Exception as error:
        report['error'] = str(error)
    finally:
        if sid:
            try:
                current = operator.request('/api/state')['session']
                if current and current['id'] == sid and current['status'] != 'ended':
                    operator.action('reset', sid)
                    operator.wait(lambda s:s['session']['status']=='ended', 'cleanup reset', sid=sid)
                report['cleanup'] = 'owned investigation ended'
            except Exception as error:
                report['cleanup'] = 'UNRESOLVED: '+str(error)
        (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))
        print('Artifacts:', output)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
