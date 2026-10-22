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
from collections import Counter
from pathlib import Path
import time
import uuid
from urllib.request import urlopen

from validate_f1 import Operator, BASE

ROOT = Path(__file__).resolve().parents[1]
NODES = ('gateway-a', 'catalog-b', 'gateway-c')
# A different node to try a wrong remedy on.
OTHER = {'gateway-a': 'catalog-b', 'catalog-b': 'gateway-c', 'gateway-c': 'gateway-a'}
KINDS = ('service-unavailable', 'cpu-starvation', 'disk-full', 'config-drift', 'network-delay', 'packet-loss', 'quota-zero', 'bad-release', 'memory-leak',
         'traffic-flood', 'retry-storm', 'power-loss', 'thermal-throttle', 'dns-failure')
REMEDY = {'service-unavailable': 'start', 'cpu-starvation': 'calm', 'disk-full': 'clear', 'config-drift': 'restore', 'network-delay': 'undelay',
          'packet-loss': 'undrop', 'quota-zero': 'requota', 'bad-release': 'rollback', 'memory-leak': 'rollback', 'traffic-flood': 'ratelimit', 'retry-storm': 'shed', 'power-loss': 'breaker', 'thermal-throttle': 'uncap',
          'dns-failure': 'resolver'}
TARGETS = {'quota-zero': ('catalog-b',), 'traffic-flood': ('catalog-b',), 'retry-storm': ('gateway-a', 'gateway-c'), 'power-loss': ('catalog-b',), 'thermal-throttle': ('catalog-b',),
           'dns-failure': ('gateway-a', 'gateway-c')}  # faults that only exist on some services
# The fix for the fault each one is most easily mistaken for: it must change nothing.
LOOKALIKE = {'packet-loss': 'undelay', 'network-delay': 'undrop', 'quota-zero': 'restore', 'bad-release': 'restore', 'traffic-flood': 'calm', 'retry-storm': 'ratelimit', 'thermal-throttle': 'calm',
             'dns-failure': 'restore'}
INTERMITTENT = ('cpu-starvation', 'packet-loss', 'bad-release', 'memory-leak')
RELEASE_FAULTS = {'v1.5.0': 'bad-release', 'v1.5.1': 'memory-leak'}  # effects that come and go from one cycle to the next


def expected(kind, target):
    """Usual health right after detection. A catalogue fault reaches both gateways (through failure or
    slowness); a gateway fault stays on that gateway."""
    if kind == 'thermal-throttle':  # every service capped; the gateways, doing the most work per request, fail first
        return {'gateway-a': 'degraded', 'catalog-b': 'healthy', 'gateway-c': 'degraded'}
    if kind == 'power-loss':  # rack R1 (the catalogue and gateway-a) loses power; gateway-c runs but cannot reach the catalogue
        return {'gateway-a': 'unavailable', 'catalog-b': 'unavailable', 'gateway-c': 'degraded'}
    if kind == 'quota-zero':  # the catalogue refuses its callers but answers its own probes
        return {n: 'healthy' if n == target else 'degraded' for n in NODES}
    if target == 'catalog-b':
        return {n: 'unavailable' if n == target and kind == 'service-unavailable' else 'degraded' for n in NODES}
    return {n: ('unavailable' if kind == 'service-unavailable' else 'degraded') if n == target else 'healthy' for n in NODES}


def check(operator, node, kind):
    """Run one check; a failed inspection is run once more, as a learner would when a lossy reply times out."""
    result = operator.request('/api/check', {'node': node, 'kind': kind})
    if not result['body'].get('ok') and kind not in ('liveness', 'readiness', 'business'):
        result = operator.request('/api/check', {'node': node, 'kind': kind})
    return result


def export(sid):
    with urlopen(f'{BASE}/api/export?session_id={sid}', timeout=25) as response:
        return json.load(response)


def owned_active(operator):
    """The id of the investigation still active, if any (callers only start runs when none was active)."""
    try:
        session = operator.request('/api/state')['session'] or {}
    except RuntimeError:
        return None
    return session.get('id') if session.get('status') not in (None, 'ended') else None


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
    """Inspect every node five ways and report what the evidence shows, without the fault record."""
    found, evidence = [], {}
    for node in NODES:
        service, resources, disk, config, network, traffic, releases, memory, facility = (
            check(operator, node, k) for k in ('service', 'resources', 'disk', 'config', 'network', 'traffic', 'releases', 'memory', 'facility'))
        evidence[node] = {'service': service, 'resources': resources, 'disk': disk, 'config': config, 'network': network, 'traffic': traffic,
                          'releases': releases, 'memory': memory, 'facility': facility}
        if (facility['body'].get('power_cap') or {}).get('active'):
            if node == 'catalog-b':  # one cap on the whole room is one fault, named by the catalogue
                found.append(('thermal-throttle', node, facility))
            continue
        if facility['body'].get('breaker') == 'open':
            # One power feed down is one fault, however many services it feeds; it is named by the catalogue's rack.
            if node == 'catalog-b' or 'catalog-b' not in facility['body'].get('fed_services', []):
                found.append(('power-loss', node, facility))
            continue
        queues = network['body']['result'].get('qdiscs', []) if network['body'].get('ok') else []
        dns = (network['body'].get('result') or {}).get('dns') if network['body'].get('ok') else None
        # A failed lookup is a broken resolver only when the resolver changed or the service behind the name still answers;
        # a stopped catalogue's name fails too, because Docker's resolver drops it.
        broken_resolver = bool(dns and dns.get('ok') is False and (dns.get('changed') or dns.get('connect_last_good_ms') is not None))
        running_release = (releases['body'].get('result') or {}).get('running') if releases['body'].get('ok') else None
        growth = ((memory['body'].get('result') or {}).get('growth_mib_s') or 0) if memory['body'].get('ok') else 0
        heavy = [c for c in (traffic['body'].get('result') or {}).get('clients', []) if (c.get('recent_rps') or 0) > 20 and c['client'] not in
                 {q['client'] for q in (traffic['body'].get('result') or {}).get('quotas', [])} and c['client'] != 'controller'] if traffic['body'].get('ok') else []
        clients = (traffic['body'].get('result') or {}).get('clients', []) if traffic['body'].get('ok') else []
        stormy = [c for c in clients if (c.get('recent_rps') or 0) > 20 and (c.get('recent_retries') or 0) / ((c.get('recent_rps') or 1) * 10) > 0.5]
        heavy = [c for c in heavy if c not in stormy] if node == 'catalog-b' else []  # heavy users at a gateway are its traffic, not a flood
        if memory['body'].get('oom_killed') or growth >= 4:
            found.append(('memory-leak', node, memory))
        elif stormy:
            found.append(('retry-storm', node, traffic))
        elif heavy:
            found.append(('traffic-flood', node, traffic))
        elif running_release in RELEASE_FAULTS:
            found.append((RELEASE_FAULTS[running_release], node, releases))
        elif service['body'].get('running') is False:
            found.append(('service-unavailable', node, service))
        elif (resources['body'].get('cpu_pct_of_limit') or 0) >= 80:
            found.append(('cpu-starvation', node, resources))
        elif disk['body'].get('ok') and disk['body']['result']['free_bytes'] < 64 * 1024:
            found.append(('disk-full', node, disk))
        elif config['body'].get('ok') and config['body']['result']['drifted']:
            found.append(('config-drift', node, config))
        elif broken_resolver:
            found.append(('dns-failure', node, network))
        elif any(q['kind'] == 'netem' and q.get('loss_pct') for q in queues):
            found.append(('packet-loss', node, network))
        elif any(q['kind'] == 'netem' for q in queues):
            found.append(('network-delay', node, network))
        elif traffic['body'].get('ok') and any(q['limit_rps'] == 0 for q in traffic['body']['result'].get('quotas', [])):
            found.append(('quota-zero', node, traffic))
    return found, evidence


def diagnose(operator, sid, state, mode):
    if mode == 'challenge':
        ensure('scenario' not in state['session'], 'challenge state leaked the scenario')
        ensure('fault_target' not in json.dumps(export(sid)) and 'fault_kind' not in json.dumps(export(sid)), 'challenge export leaked the fault')
    # A crash-looping service can be between restarts with nothing yet to see, and a process that has just started
    # uses its whole CPU limit while it boots, which is not starvation. Look again, as a learner would.
    for _ in range(6):
        found, evidence = anomalies(operator)
        booting = {n for n, e in evidence.items() if e['memory']['body'].get('running')
                   and (e['memory']['body'].get('service_error') or ((e['memory']['body'].get('result') or {}).get('uptime_s') or 99) < 15)}
        # CPU at a gateway's limit with heavy app users may be a retry storm still building; look again before calling it starvation.
        busy_users = {n for n, e in evidence.items() if any((c.get('recent_rps') or 0) > 20 for c in ((e['traffic']['body'].get('result') or {}).get('clients', [])) if c['client'] != 'controller')}
        if found and not any(k == 'cpu-starvation' and (n in booting or n in busy_users) for k, n, _ in found):
            break
        time.sleep(4)
    ensure(len(found) == 1, f'expected exactly one anomaly, saw {[(k, n) for k, n, _ in found]}')
    kind, target, inspection = found[0]
    # Health is a per-cycle sample and CPU starvation is statistical, so judge the usual reading over several cycles.
    samples = []
    for _ in range(10 if kind in ('packet-loss', 'bad-release', 'memory-leak') else 5):  # about half their cycles fail, so take more samples
        samples.append({n['id']: n['health'] for n in operator.request('/api/state')['nodes']})
        time.sleep(2)
    # A healthy node must read healthy most of the time; an affected node must show its effect in at least two of
    # five cycles (a CPU-starved service flickers in and out of "slow", which is real behaviour, not a test fault).
    want = expected(kind, target)
    affected = [node for node in NODES if want[node] != 'healthy']
    for node in NODES:
        readings = Counter(sample[node] for sample in samples)
        if want[node] == 'healthy':
            ok = readings.most_common(1)[0][0] == 'healthy'
        elif kind in INTERMITTENT:
            # Starvation slows, and packet loss delays or fails, whichever request it lands on: in a given cycle that
            # may be the catalogue's own probe or only the gateways' calls through it. Judge the affected group as a whole.
            ok = sum(any(sample[n] != 'healthy' for n in affected) for sample in samples) >= 2
        else:
            ok = readings[want[node]] >= 2
        ensure(ok, f'unexpected measured effects for {kind} on {target}: {node} expected {want[node]}, saw {dict(readings)} in {samples}')
    watched = target if target.startswith('gateway') else 'gateway-a'  # a gateway the fault reaches
    business = check(operator, watched, 'business')
    # CPU starvation is statistical: judge the recent gateway requests, not one sample.
    # Judge the gateway's last 20 business requests from the investigation itself; the page's 60 newest records are mostly
    # the inspections just run, which left as few as nine requests to judge an intermittent fault by.
    # A catalogue fault reaches both gateways, so judge them together.
    callers = (watched,) if target.startswith('gateway') else ('gateway-a', 'gateway-c')
    recent = [e for node in callers for e in operator.request(f'/api/investigations/{sid}/evidence?node={node}&kind=business&limit=20')['items']]
    affected = sum(1 for e in recent if e['body']['ok'] is False or e['body'].get('slow'))
    # Packet loss leaves most requests working, which is the point; the others mostly fail or are slow.
    floor = len(recent) // 10 if kind in ('packet-loss', 'bad-release', 'memory-leak') else len(recent) // 2
    ensure(affected >= floor, f'gateway business requests should be affected; {affected}/{len(recent)} were')
    # Guidance only trusts inspections from the last 30 seconds, and a full sweep plus sampling takes longer than that,
    # so repeat the telling inspection first, as a learner would. A crash loop's evidence depends on the moment, so
    # its original record stands.
    if kind != 'memory-leak' and time.time() - inspection['at'] > 15:
        inspection = check(operator, target, inspection['kind'])
    action = operator.request('/api/help', {'session_id': sid, 'level': 'action'})
    ensure(target in action['message'] and inspection['id'] in action['evidence_ids'], f'action guidance did not cite the {kind} evidence: {action["message"]}')
    note = operator.request('/api/note', {'session_id': sid, 'explanation': f'{kind} on {target}, from inspections.',
                                         'uncertainty': 'Evidence does not show why it happened.', 'evidence_ids': [inspection['id'], business['id']]})
    ensure(note.get('saved'), 'diagnosis note was not saved')
    return kind, target, [inspection['id'], business['id']]


def recover(operator, sid, kind, target):
    # A wrong node, and (while the target still runs) a wrong kind of remedy, must both change nothing.
    # Starting a crash-looping service really does start it, so for a leak the test is that it is not credited.
    wrong = ([(OTHER[target], REMEDY[kind])] + ([(target, 'start')] if kind not in ('service-unavailable', 'memory-leak') else [])
             + ([(target, LOOKALIKE[kind])] if kind in LOOKALIKE else []))
    for node, verb in wrong:
        wrong = operator.action('recover', sid, node=node, verb=verb)
        ensure(wrong['status'] == 'unchanged', f'{verb} on {node} should change nothing, got {wrong["status"]}')
    time.sleep(7)
    ensure(operator.request('/api/state')['session']['status'] != 'recovered', 'ineffective attempts were credited as recovery')
    if kind == 'thermal-throttle':
        # The cap must not lift while the room is hot; once the facility reports cooling restored, it may.
        early = operator.action('recover', sid, node=target, verb='uncap')
        ensure(early['status'] == 'unchanged' and 'Inlet still' in early.get('detail', ''), f'the cap lifted while the room was hot: {early}')
        operator.wait(lambda s: check(operator, target, 'facility')['body']['cooling']['crac_2'] == 'running', 'cooling restored', sid=sid, timeout=120)
    began = time.monotonic()
    right = operator.action('recover', sid, node=target, verb=REMEDY[kind])
    ensure(right['status'] == 'succeeded', f'{REMEDY[kind]} on {target} not confirmed: {right}')
    state = operator.wait(lambda s: s['session']['status'] == 'recovered', 'verified recovery', sid=sid)
    ensure(len(state['session']['recovery_evidence_ids']) == 6 * len(NODES), 'recovery should cite three cycles of every node')
    flapping = state['session'].get('flaps', 0) >= 1
    ensure(state['session'].get('required_streak', 3) == (10 if flapping else 3), 'ten healthy cycles are required exactly when failures came and went on their own')
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
    parser.add_argument('--kinds', help='Comma-separated fault kinds to run in practice only, skipping the challenge rounds')
    parser.add_argument('--challenge', action='store_true', help='With --kinds, still run the challenge rounds over every fault')
    args = parser.parse_args()
    if not args.run:
        parser.error('--run is required; this interrupts project services')
    kinds = tuple(args.kinds.split(',')) if args.kinds else KINDS
    if not set(kinds) <= set(KINDS):
        parser.error('unknown fault kind in --kinds')
    operator = Operator()
    state = operator.request('/api/state')
    if state['session'] and state['session']['status'] != 'ended':
        parser.error('An investigation is already active; finish it first')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'rounds': [], 'passed': False}
    sid = None
    try:
        for kind in kinds:
            for target in TARGETS.get(kind, NODES):
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
        challenge = not args.kinds or args.challenge
        for attempt in range(64 if challenge else 0):
            if kinds_seen == set(KINDS) and targets_seen == set(NODES):
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
        ensure(not challenge or (kinds_seen == set(KINDS) and targets_seen == set(NODES)), f'challenge only selected {kinds_seen} on {targets_seen}')
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
        # A round can fail before its id is known (detection timed out); this script refuses to start beside another
        # investigation, so an active one now is its own.
        sid = sid or owned_active(operator)
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
