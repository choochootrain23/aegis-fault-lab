"""Production gates against the running Docker lab; explicit --run required.

mutation-restart: kill the controller while a recovery start is in flight, restart it,
and confirm the action is marked unknown, never replayed, and verified only by fresh checks.
lease: run the unaccelerated 15-minute timer with no browser attached and confirm the
automatic verified reset, while sampling container and host resources.
Only project containers are touched. Reports go to ignored state/validation/.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from validate_f1 import Operator

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'aegis-rack-diagnostics'


def compose(*args):
    return subprocess.run(['docker', 'compose', *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def start_events(service, since, until):
    out = subprocess.run(['docker', 'events', '--since', str(since), '--until', str(until), '--format', '{{.Action}}',
                          '--filter', f'container={PROJECT}-{service}-1', '--filter', 'event=start'],
                         check=True, capture_output=True, text=True).stdout
    return len(out.split())


def wait_reachable(operator, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return operator.request('/api/state')
        except RuntimeError:
            time.sleep(0.5)
    raise RuntimeError('Controller did not return after restart')


def begin(operator):
    operator.wait(lambda s: s['baseline_cycles'] >= 5 and all(n['health'] == 'healthy' for n in s['nodes']), 'healthy baseline', timeout=90)
    session = operator.request('/api/sessions', {'mode': 'practice', 'target': 'catalog-b', 'key': uuid.uuid4().hex})
    operator.wait(lambda s: s['session']['status'] == 'investigating', 'fault detection', sid=session['id'])
    return session


def finish(operator, sid):
    current = operator.request('/api/state')['session']
    if current and current['id'] == sid and current['status'] != 'ended':
        operator.action('reset', sid)
        operator.wait(lambda s: s['session']['status'] == 'ended', 'verified reset', sid=sid, timeout=60)


def mutation_restart(operator, report):
    # The window between recorded intent and broker completion is short; vary the kill delay until it lands inside.
    for attempt, delay in enumerate((0.15, 0.08, 0.25, 0.04, 0.35, 0.02, 0.5), 1):
        sid = begin(operator)['id']
        before = operator.request('/api/state')['session']
        since = time.time()
        sent = threading.Thread(target=lambda: _ignore(lambda: operator.action('recover', sid, node='catalog-b')))
        sent.start()
        time.sleep(delay)
        compose('kill', 'controller')
        sent.join(timeout=30)
        compose('start', 'controller')
        state = wait_reachable(operator)
        attempts = state['session']['recovery_attempts'] if state['session'] and state['session']['id'] == sid else None
        if attempts is None:
            raise RuntimeError('Active investigation was not restored after controller restart')
        new = attempts[len(before['recovery_attempts']):]
        outcome = new[0]['status'] if new else 'not_recorded'
        trial = {'attempt': attempt, 'kill_delay_s': delay, 'recorded_status': outcome}
        report['trials'].append(trial)
        if outcome != 'unknown':
            # Killed before intent or after completion: not the in-flight case. Restore and retry.
            finish(operator, sid)
            continue
        time.sleep(4)
        trial['broker_start_events'] = start_events('catalog-b', since, time.time())
        if trial['broker_start_events'] > 1:
            raise RuntimeError('Recovery start was replayed after restart')
        if 'restarted during action' not in (new[0].get('detail') or ''):
            raise RuntimeError('Unknown action lacks the restart reconciliation detail')
        running = operator.request('/api/check', {'node': 'catalog-b', 'kind': 'service'})['body']['running']
        trial['catalog_running_after_restart'] = running
        if not running:
            # The broker never completed the start. The operator must act explicitly; nothing was replayed.
            operator.action('recover', sid, node='catalog-b')
        recovered = operator.wait(lambda s: s['session']['status'] == 'recovered', 'verified recovery after restart', sid=sid, timeout=60)
        trial['outcome'] = recovered['session']['outcome']
        trial['recovery_evidence'] = len(recovered['session'].get('recovery_evidence_ids', []))
        finish(operator, sid)
        return True
    raise RuntimeError('Kill never landed inside the mutation window; no in-flight case observed')


def _ignore(call):
    try:
        call()
    except RuntimeError:
        pass


def sample(operator):
    stats = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{json .}}'], check=True, capture_output=True, text=True).stdout
    containers = {}
    for line in stats.splitlines():
        row = json.loads(line)
        if row['Name'].startswith(PROJECT + '-'):
            containers[row['Name'][len(PROJECT) + 1:-2]] = {'cpu': row['CPUPerc'], 'mem': row['MemUsage'].split(' / ')[0]}
    meminfo = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    kib = lambda key: int(meminfo[key].split()[0])
    state = operator.request('/api/state')
    return {'at': round(time.time()), 'containers': containers, 'host_available_mib': kib('MemAvailable') // 1024,
            'host_swap_used_mib': (kib('SwapTotal') - kib('SwapFree')) // 1024,
            'db_live_mib': round(state['storage']['live_bytes'] / 2**20, 2), 'session_status': state['session']['status']}


def lease(operator, report):
    session = begin(operator)
    sid = session['id']
    report['deadline'] = session['deadline']
    report['samples'] = []
    while True:
        state = operator.request('/api/state')
        s = state['session']
        if s['id'] != sid:
            raise RuntimeError('Investigation changed unexpectedly')
        if s['status'] == 'ended':
            break
        if time.time() > session['deadline'] + 150:
            raise RuntimeError('Automatic reset did not complete within 90 s of the grace period ending')
        if not report['samples'] or time.time() - report['samples'][-1]['at'] >= 60:
            report['samples'].append(sample(operator))
            print('sample', json.dumps(report['samples'][-1]), flush=True)
        time.sleep(2)
    ended = time.time()
    report.update(closure=s['closure'], outcome=s['outcome'], reset_automatic=s['reset']['automatic'],
                  ended_after_deadline_s=round(ended - session['deadline'], 1),
                  reset_evidence=len(s.get('reset_evidence_ids', [])),
                  health_after=[n['health'] for n in state['nodes']])
    if s['closure'] != 'automatic_reset' or not s['reset']['automatic'] or report['reset_evidence'] != 12:
        raise RuntimeError('Expiry did not produce a verified automatic reset')
    report['samples'].append(sample(operator))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gate', choices=('mutation-restart', 'lease'))
    parser.add_argument('--run', action='store_true', help='Authorize stopping project services and killing the project controller')
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; this interrupts project services')
    operator = Operator()
    state = operator.request('/api/state')
    if state['session'] and state['session']['status'] != 'ended':
        parser.error('An investigation is already active; finish it first')
    report = {'gate': args.gate, 'started_at': datetime.now(timezone.utc).isoformat(), 'trials': [], 'passed': False}
    try:
        report['passed'] = (mutation_restart if args.gate == 'mutation-restart' else lease)(operator, report)
    except Exception as error:
        report['error'] = str(error)
        print('GATE FAILED:', error)
    finally:
        output = ROOT / 'state' / 'validation' / f'{args.gate}-{uuid.uuid4().hex}.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + '\n')
        print('Report:', output, '· passed' if report['passed'] else '· FAILED')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
