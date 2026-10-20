"""Scenario matrix against the running Docker lab; explicit --run required.

Exercises every implemented scenario through the operator API: practice for each fault kind
on each target, challenge until every kind and both targets have been seen, and reset without
recovery. Each round identifies the fault from evidence alone, checks guidance, confirms that a
wrong remedy changes nothing, verifies recovery, notes, export secrecy and baseline restoration.
Reports go to ignored state/validation/.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid
from urllib.request import urlopen

from validate_f1 import Operator, BASE

ROOT = Path(__file__).resolve().parents[1]
OTHER = {'gateway-a': 'catalog-b', 'catalog-b': 'gateway-a'}
KINDS = ('service-unavailable', 'cpu-starvation', 'disk-full')
REMEDY = {'service-unavailable': 'start', 'cpu-starvation': 'calm', 'disk-full': 'clear'}
# Health right after detection. The slow catalogue can also make gateway requests time out,
# so the gateway may read degraded through either slowness or failure.
EXPECTED = {('service-unavailable', 'catalog-b'): {'gateway-a': 'degraded', 'catalog-b': 'unavailable'},
            ('service-unavailable', 'gateway-a'): {'gateway-a': 'unavailable', 'catalog-b': 'healthy'},
            ('cpu-starvation', 'catalog-b'): {'gateway-a': 'degraded', 'catalog-b': 'degraded'},
            ('cpu-starvation', 'gateway-a'): {'gateway-a': 'degraded', 'catalog-b': 'healthy'},
            ('disk-full', 'catalog-b'): {'gateway-a': 'degraded', 'catalog-b': 'degraded'},
            ('disk-full', 'gateway-a'): {'gateway-a': 'degraded', 'catalog-b': 'healthy'}}


def check(operator, node, kind):
    return operator.request('/api/check', {'node': node, 'kind': kind})


def export(sid):
    with urlopen(f'{BASE}/api/export?session_id={sid}', timeout=25) as response:
        return json.load(response)


def ensure(condition, message):
    if not condition:
        raise RuntimeError(message)


def start(operator, mode, target=None, kind=None):
    operator.wait(lambda s: s['baseline_cycles'] >= 5 and all(n['health'] == 'healthy' for n in s['nodes']), 'healthy baseline', timeout=90)
    payload = {'mode': mode, 'key': uuid.uuid4().hex, **({'target': target, 'kind': kind or 'service-unavailable'} if target else {})}
    session = operator.request('/api/sessions', payload)
    began = time.monotonic()
    state = operator.wait(lambda s: s['session']['status'] == 'investigating', 'fault detection', sid=session['id'])
    return session['id'], state, round(time.monotonic() - began, 2)


def anomalies(operator):
    """Inspect both nodes three ways and report what the evidence shows, without the fault record."""
    found, evidence = [], {}
    for node in OTHER:
        service, resources, disk = (check(operator, node, k) for k in ('service', 'resources', 'disk'))
        evidence[node] = {'service': service, 'resources': resources, 'disk': disk}
        if service['body'].get('running') is False:
            found.append(('service-unavailable', node, service))
        elif (resources['body'].get('cpu_pct_of_limit') or 0) >= 80:
            found.append(('cpu-starvation', node, resources))
        elif disk['body'].get('ok') and disk['body']['result']['free_bytes'] < 64 * 1024:
            found.append(('disk-full', node, disk))
    return found, evidence


def diagnose(operator, sid, state, mode):
    if mode == 'challenge':
        ensure('scenario' not in state['session'], 'challenge state leaked the scenario')
        ensure('fault_target' not in json.dumps(export(sid)) and 'fault_kind' not in json.dumps(export(sid)), 'challenge export leaked the fault')
    found, _ = anomalies(operator)
    ensure(len(found) == 1, f'expected exactly one anomaly, saw {[(k, n) for k, n, _ in found]}')
    kind, target, inspection = found[0]
    health = {n['id']: n['health'] for n in operator.request('/api/state')['nodes']}
    ensure(health == EXPECTED[(kind, target)], f'unexpected measured effects for {kind} on {target}: {health}')
    business = check(operator, 'gateway-a', 'business')
    # CPU starvation is statistical: judge the recent gateway requests, not one sample.
    recent = [e for e in operator.request('/api/state')['evidence'] if e['node'] == 'gateway-a' and e['kind'] == 'business'][:10]
    affected = sum(1 for e in recent if e['body']['ok'] is False or e['body'].get('slow'))
    ensure(affected >= len(recent) // 2, f'gateway business requests should mostly fail or be slow; {affected}/{len(recent)} were affected')
    action = operator.request('/api/help', {'session_id': sid, 'level': 'action'})
    ensure(target in action['message'] and inspection['id'] in action['evidence_ids'], f'action guidance did not cite the {kind} evidence: {action["message"]}')
    note = operator.request('/api/note', {'session_id': sid, 'explanation': f'{kind} on {target}, from inspections.',
                                         'uncertainty': 'Evidence does not show why it happened.', 'evidence_ids': [inspection['id'], business['id']]})
    ensure(note.get('saved'), 'diagnosis note was not saved')
    return kind, target, [inspection['id'], business['id']]


def recover(operator, sid, kind, target):
    # A wrong node, and (while the target still runs) a wrong kind of remedy, must both change nothing.
    wrong = [(OTHER[target], REMEDY[kind])] + ([(target, 'start')] if kind != 'service-unavailable' else [])
    for node, verb in wrong:
        wrong = operator.action('recover', sid, node=node, verb=verb)
        ensure(wrong['status'] == 'unchanged', f'{verb} on {node} should change nothing, got {wrong["status"]}')
    time.sleep(7)
    ensure(operator.request('/api/state')['session']['status'] != 'recovered', 'ineffective attempts were credited as recovery')
    began = time.monotonic()
    right = operator.action('recover', sid, node=target, verb=REMEDY[kind])
    ensure(right['status'] == 'succeeded', f'{REMEDY[kind]} on {target} not confirmed: {right}')
    state = operator.wait(lambda s: s['session']['status'] == 'recovered', 'verified recovery', sid=sid)
    ensure(len(state['session']['recovery_evidence_ids']) == 12, 'recovery should cite twelve observations')
    return round(time.monotonic() - began, 2)


def close(operator, sid, cited, mode):
    if mode == 'challenge':
        revealed = operator.action('reveal', sid)
        ensure(revealed.get('scenario'), 'reveal did not return the scenario')
    operator.action('reset', sid)
    state = operator.wait(lambda s: s['session']['status'] == 'ended', 'verified reset', sid=sid, timeout=60)
    ensure(all(n['health'] == 'healthy' for n in state['nodes']), 'baseline not healthy after reset')
    exported = export(sid)
    ensure({e['id'] for e in exported['evidence']} >= set(cited), 'export omitted cited evidence')
    ensure(exported['session'].get('scenario'), 'ended export should include the scenario debrief')
    return state['session']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Authorize stopping and starting the two project services')
    if not parser.parse_args().run:
        parser.error('--run is required; this interrupts project services')
    operator = Operator()
    state = operator.request('/api/state')
    if state['session'] and state['session']['status'] != 'ended':
        parser.error('An investigation is already active; finish it first')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'rounds': [], 'passed': False}
    sid = None
    try:
        for kind in KINDS:
            for target in OTHER:
                sid, state, detect = start(operator, 'practice', target, kind)
                ensure(state['session']['scenario'] == {'kind': kind, 'target': target}, 'practice scenario not shown')
                found_kind, found, cited = diagnose(operator, sid, state, 'practice')
                ensure((found_kind, found) == (kind, target), f'evidence pointed at {found_kind} on {found}, expected {kind} on {target}')
                recovery = recover(operator, sid, kind, target)
                ended = close(operator, sid, cited, 'practice')
                report['rounds'].append({'mode': 'practice', 'kind': kind, 'target': target, 'detect_s': detect, 'recovery_s': recovery, 'outcome': ended['outcome']})
                print(f'practice {kind} {target}: detect {detect}s, recovery {recovery}s, {ended["outcome"]}', flush=True)
                sid = None
        kinds_seen, targets_seen = set(), set()
        for attempt in range(14):
            if kinds_seen == set(KINDS) and targets_seen == set(OTHER):
                break
            sid, state, detect = start(operator, 'challenge')
            kind, found, cited = diagnose(operator, sid, state, 'challenge')
            recovery = recover(operator, sid, kind, found)
            ended = close(operator, sid, cited, 'challenge')
            ensure(ended['scenario'] == {'kind': kind, 'target': found}, f'evidence-based diagnosis {kind} on {found} disagrees with the revealed {ended["scenario"]}')
            kinds_seen.add(kind); targets_seen.add(found)
            report['rounds'].append({'mode': 'challenge', 'kind': kind, 'target': found, 'detect_s': detect, 'recovery_s': recovery, 'outcome': ended['outcome']})
            print(f'challenge {kind} {found}: detect {detect}s, recovery {recovery}s', flush=True)
            sid = None
        ensure(kinds_seen == set(KINDS) and targets_seen == set(OTHER), f'challenge only selected {kinds_seen} on {targets_seen}')
        # Reset without any recovery closes as a reset, not a recovery.
        sid, state, detect = start(operator, 'practice', 'catalog-b')
        operator.action('reset', sid)
        ended = operator.wait(lambda s: s['session']['status'] == 'ended', 'reset without recovery', sid=sid, timeout=60)['session']
        ensure(ended['outcome'] == 'reset' and ended['closure'] == 'operator_reset' and not ended.get('recovery_verified_at'), 'reset was credited as recovery')
        report['rounds'].append({'mode': 'practice', 'target': 'catalog-b', 'detect_s': detect, 'outcome': 'reset', 'note': 'reset without recovery'})
        print('reset without recovery: outcome reset', flush=True)
        sid = None
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
        print('SCENARIO FAILED:', error)
    finally:
        if sid:
            try:
                current = operator.request('/api/state')['session']
                if current and current['id'] == sid and current['status'] != 'ended':
                    operator.action('reset', sid)
                    operator.wait(lambda s: s['session']['status'] == 'ended', 'cleanup reset', sid=sid, timeout=60)
                    report['cleanup'] = 'baseline verified'
            except Exception as error:
                report['cleanup'] = 'UNRESOLVED: ' + str(error)
        output = ROOT / 'state' / 'validation' / f'scenarios-{uuid.uuid4().hex}.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + '\n')
        print('Report:', output, '· passed' if report['passed'] else '· FAILED')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
