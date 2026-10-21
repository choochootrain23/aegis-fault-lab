"""Multi-hour soak run against the running Docker lab; explicit --run required.

Repeats random practice and challenge investigations (diagnose from evidence, recover, reset) for the
given number of hours, sampling controller and broker memory, evidence storage and collector errors
after every round. Stops at the first failed round. Reports go to ignored state/validation/.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import subprocess
import time
import uuid

from validate_scenarios import KINDS, NODES, Operator, close, diagnose, recover, start

ROOT = Path(__file__).resolve().parents[1]


def memory():
    """Memory per project container in MiB, from docker stats."""
    out = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{.Name}} {{.MemUsage}}'], capture_output=True, text=True, timeout=30).stdout
    usage = {}
    for line in out.splitlines():
        name, used = line.split(' ', 1)
        if name.startswith('aegis-rack-diagnostics-'):
            value, unit = float(''.join(c for c in used.split('/')[0] if c.isdigit() or c == '.')), used.split('/')[0].strip()[-3:]
            usage[name.removeprefix('aegis-rack-diagnostics-').removesuffix('-1')] = round(value / 1024 if unit == 'GiB' else value, 1)
    return usage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Authorize repeatedly breaking and recovering the project services')
    parser.add_argument('--hours', type=float, default=2.0)
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; this interrupts project services for hours')
    operator = Operator()
    if (operator.request('/api/state')['session'] or {}).get('status', 'ended') != 'ended':
        parser.error('An investigation is already active; finish it first')
    output = ROOT / 'state' / 'validation' / f'soak-{uuid.uuid4().hex}.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'hours': args.hours, 'rounds': [], 'passed': False}
    deadline = time.monotonic() + args.hours * 3600
    sid = None
    try:
        while time.monotonic() < deadline:
            mode = 'challenge' if random.random() < 0.4 else 'practice'
            target, kind = (None, None) if mode == 'challenge' else (random.choice(NODES), random.choice(KINDS))
            sid, state, detect = start(operator, mode, target, kind)
            found_kind, found, cited = diagnose(operator, sid, state, mode)
            recovery = recover(operator, sid, found_kind, found)
            ended = close(operator, sid, cited, mode)
            sid = None
            if mode == 'practice':
                assert (found_kind, found) == (kind, target), f'evidence pointed at {found_kind} on {found}, expected {kind} on {target}'
            else:
                assert ended['scenario'] == {'kind': found_kind, 'target': found}, 'challenge diagnosis disagreed with the reveal'
            current = operator.request('/api/state')
            sample = {'at': round(time.time()), 'mode': mode, 'kind': found_kind, 'target': found, 'detect_s': detect, 'recovery_s': recovery,
                      'memory_mib': memory(), 'live_mib': round(current['storage']['live_bytes'] / 2**20, 2),
                      'wal_mib': round(current['storage']['wal_bytes'] / 2**20, 2), 'collector_error': current['collector_error']}
            assert sample['collector_error'] is None, sample['collector_error']
            report['rounds'].append(sample)
            output.write_text(json.dumps(report, indent=2) + '\n')
            print(f"{len(report['rounds']):3d} {mode:9} {found_kind:20} {found:9} detect {detect:5.1f}s recover {recovery:5.1f}s "
                  f"controller {sample['memory_mib'].get('controller')} MiB db {sample['live_mib']} MiB", flush=True)
        report['passed'] = True
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        print('SOAK FAILED:', report['error'])
    finally:
        if sid:
            try:
                operator.action('reset', sid)
                operator.wait(lambda s: s['session']['status'] == 'ended', 'cleanup reset', sid=sid, timeout=60)
                report['cleanup'] = 'baseline verified'
            except Exception as error:
                report['cleanup'] = 'UNRESOLVED: ' + str(error)
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(report, indent=2) + '\n')
        print('Report:', output, '· passed' if report['passed'] else '· FAILED')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
