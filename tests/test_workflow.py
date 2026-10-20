import asyncio
import json
import time

import httpx
import pytest
from fastapi import HTTPException

from aegis.controller import Lab, Start
from aegis.store import Store


class Fleet:
    """Test double only; never wired into the application runtime."""
    def __init__(self):
        self.running = {'gateway-a': True, 'catalog-b': True}
        self.busy = {'gateway-a': False, 'catalog-b': False}
        self.full = {'gateway-a': False, 'catalog-b': False}
        self.drifted = {'gateway-a': False, 'catalog-b': False}
        self.mutations = []
        self.broker_available = True

    def broker(self, op):
        node, verb = op['target'], op['verb']
        changed = False
        if verb not in ('inspect', 'resources'):
            if op['generation'] != node + '-generation':
                return httpx.Response(409)
            changed = self.running[node] != (verb == 'start')
            self.running[node] = verb == 'start'
            if changed:
                self.mutations.append((node, verb))
        return httpx.Response(200, json={'target': node, 'generation': node + '-generation', 'running': self.running[node],
                                         'status': 'running' if self.running[node] else 'exited', 'exit_code': 0, 'changed': changed,
                                         **({'cpu_limit': 0.25, 'cpu_pct_of_limit': 100.0 if self.busy[node] else 4.0} if verb == 'resources' else {})})

    def handle(self, request):
        if request.url.host == 'broker':
            if not self.broker_available:
                return httpx.Response(503)
            return self.broker(json.loads(request.content))
        node, path = request.url.host, request.url.path
        if not self.running[node]:
            raise httpx.ConnectError('Process stopped', request=request)
        if path.startswith('/maintenance/'):
            if request.headers.get('x-aegis-fleet-token') != 'fleet-token':
                return httpx.Response(403)
            verb = path.rsplit('/', 1)[1]
            state = {'fill': self.full, 'clear': self.full, 'burn': self.busy, 'calm': self.busy, 'drift': self.drifted, 'restore': self.drifted}[verb]
            changed = state[node] != (verb in ('fill', 'burn', 'drift'))
            state[node] = verb in ('fill', 'burn', 'drift')
            if changed:
                self.mutations.append((node, path.rsplit('/', 1)[1]))
            return httpx.Response(200, json={'changed': changed})
        if path == '/diagnostics/disk':
            return httpx.Response(200, json={'free_bytes': 0 if self.full[node] else 8_000_000, 'largest': []})
        if path == '/diagnostics/config':
            drifted = ['upstream'] if self.drifted[node] else []
            return httpx.Response(200, json={'drifted': drifted, 'settings': [{'key': 'upstream', 'value': 'http://catalog-b:8080' if drifted else 'http://catalog-b:8000',
                                                                               'deployed': 'http://catalog-b:8000', 'drifted': bool(drifted)}]})
        if path == '/diagnostics/trace':
            return httpx.Response(200, json={'request_ok': not self.drifted[node], 'spans': [], 'handler_ms': 2.0})
        slow = self.busy[node] or (node == 'gateway-a' and path == '/summary' and self.busy['catalog-b'])
        if slow and path in ('/live', '/summary'):
            time.sleep(0.06)  # above the test SLOW_MS
        if path == '/live':
            return httpx.Response(200, json={'live': True})
        if self.full[node]:
            return httpx.Response(507, json={'ok': False, 'errno': 'ENOSPC'})
        if self.drifted[node]:
            return httpx.Response(503 if node == 'gateway-a' else 500, json={'ok': False, 'error': 'ConnectError' if node == 'gateway-a' else 'FileNotFoundError'})
        if node == 'gateway-a' and self.drifted['catalog-b']:
            return httpx.Response(503, json={'ok': False, 'upstream_status': 500})
        if node == 'gateway-a' and not self.running['catalog-b']:
            return httpx.Response(503, json={'ok': False, 'dependency': 'catalog-b'})
        if node == 'gateway-a' and self.full['catalog-b']:
            return httpx.Response(503, json={'ok': False, 'upstream_status': 507})
        return httpx.Response(200, json={'ok': True, 'dependency': 'catalog-b'})


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv('AEGIS_FLEET_TOKEN', 'fleet-token')
    monkeypatch.setattr('aegis.controller.SLOW_MS', 40)
    fleet = Fleet()
    store = Store(tmp_path / 'lab.sqlite')
    client = httpx.AsyncClient(transport=httpx.MockTransport(fleet.handle))
    result = Lab(store, client, resource_check=False)
    yield result, fleet
    run(client.aclose())
    store.db.close()


async def baseline(lab):
    for _ in range(5):
        await lab.cycle()


def test_dependency_failure_and_verified_recovery(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-001'))
        for _ in range(3):
            await controller.cycle()
        assert controller.session['status'] == 'investigating'
        states = {n['id']: n['health'] for n in controller.state()['nodes']}
        assert states == {'gateway-a': 'degraded', 'catalog-b': 'unavailable'}
        assert controller.latest[('gateway-a', 'liveness')]['body']['ok']
        result = await controller.mutate('recover-001', 'catalog-b', 'start')
        assert result['status'] == 'succeeded'
        controller.session['status'] = 'verifying'
        controller.successes = 0
        for _ in range(2):
            await controller.cycle()
            assert controller.session['status'] == 'verifying'
        await controller.cycle()
        assert controller.session['status'] == 'recovered'
        ids = controller.session['recovery_evidence_ids']
        assert len(ids) == len(set(ids)) == 12
        records = controller.store.evidence_by_ids(controller.session['id'], ids)
        assert len(records) == 12 and all(e['body']['ok'] for e in records)
    run(scenario())


def test_unknown_is_not_healthy_and_baseline_required(lab):
    async def scenario():
        controller, _ = lab
        assert all(n['health'] == 'unknown' for n in controller.state()['nodes'])
        with pytest.raises(HTTPException) as exc:
            await controller.start(Start(mode='practice', key='start-001'))
        assert exc.value.status_code == 409
    run(scenario())


def test_challenge_ground_truth_not_serialized(lab):
    async def scenario():
        controller, _ = lab
        await baseline(controller)
        await controller.start(Start(mode='challenge', key='challenge-001'))
        public = controller.state()
        for session in [public['session'], *public['history']]:
            assert 'scenario' not in session
            assert 'fault_target' not in session
            assert 'fault_action' not in session
        controller.session['revealed'] = True
        assert 'scenario' in controller.public_session()
    run(scenario())


def test_idempotency_and_key_conflict(lab):
    async def scenario():
        controller, fleet = lab
        first = await controller.mutate('action-001', 'catalog-b', 'stop')
        assert await controller.mutate('action-001', 'catalog-b', 'stop') == first
        assert len(fleet.mutations) == 1
        with pytest.raises(HTTPException):
            await controller.mutate('action-001', 'gateway-a', 'stop')
    run(scenario())


def test_expiry_resets_and_verifies_without_credit_for_recovery(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-001'))
        controller.session['deadline'] = time.time() - 61
        await controller.cycle()
        assert controller.session['status'] == 'resetting'
        assert all(fleet.running.values())
        for _ in range(3):
            await controller.cycle()
        assert controller.session['status'] == 'ended'
        assert controller.session['outcome'] == 'reset'
        assert controller.session['reset']['automatic']
    run(scenario())


def test_expiry_grace_does_not_reset_early(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-001'))
        controller.session['deadline'] = time.time() - 20
        await controller.cycle()
        assert not fleet.running['catalog-b']
    run(scenario())


def test_failed_reset_cannot_claim_baseline(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-001'))
        fleet.broker_available = False
        await controller.reset_locked('reset-001')
        for _ in range(4):
            await controller.cycle()
        assert controller.session['status'] == 'resetting'
        assert controller.session.get('outcome') is None
    run(scenario())


def test_unhelpful_recovery_keeps_business_failure(lab):
    async def scenario():
        controller, _ = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-001'))
        await controller.mutate('wrong-001', 'gateway-a', 'start')
        controller.session['status'] = 'verifying'
        for _ in range(4):
            await controller.cycle()
        assert controller.session['status'] != 'recovered'
    run(scenario())


def test_restart_preserves_unknown_action_without_replaying(lab):
    controller, fleet = lab
    controller.store.save_action('interrupted', 's1', '{}', {'status': 'running'})
    restored = Lab(controller.store, controller.client, resource_check=False)
    assert restored.store.action('interrupted')['body']['status'] == 'unknown'
    assert fleet.mutations == []


def test_broker_unavailable_prevents_injection(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        fleet.broker_available = False
        with pytest.raises(HTTPException):
            await controller.start(Start(mode='practice', key='start-001'))
        assert controller.session is None
        assert not fleet.mutations
    run(scenario())


def test_stale_measurement_is_unknown(lab):
    async def scenario():
        controller, _ = lab
        await baseline(controller)
        for item in controller.latest.values():
            item['at'] -= 10
        assert all(n['health'] == 'unknown' for n in controller.state()['nodes'])
    run(scenario())


def test_reset_retry_does_not_restart_verification(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-001'))
        await controller.reset_locked('reset-001')
        await controller.cycle()
        await controller.cycle()
        count = len(fleet.mutations)
        await controller.reset_locked('reset-001')
        assert controller.successes == 2
        assert len(fleet.mutations) == count
        await controller.cycle()
        assert controller.session['status'] == 'ended'
    run(scenario())


def test_start_idempotency_rejects_changed_intent(lab):
    async def scenario():
        controller, _ = lab
        await baseline(controller)
        first = await controller.start(Start(mode='practice', key='start-001'))
        assert await controller.start(Start(mode='practice', key='start-001')) == first
        with pytest.raises(HTTPException):
            await controller.start(Start(mode='challenge', key='start-001'))
    run(scenario())


def test_ended_investigation_stops_accruing_evidence(lab):
    async def scenario():
        controller, _ = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-001'))
        await controller.reset_locked('reset-001')
        for _ in range(3): await controller.cycle()
        count = len(controller.store.evidence(controller.session['id']))
        await controller.cycle()
        assert len(controller.store.evidence(controller.session['id'])) == count
    run(scenario())


def test_interrupted_recovery_is_visible_after_restart(lab):
    async def scenario():
        controller, _ = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-001'))
        controller.store.save_action('interrupted-recovery', controller.session['id'], '{}',
                                    {'status': 'running', 'node': 'catalog-b', 'verb': 'start'})
        restored = Lab(controller.store, controller.client, resource_check=False)
        attempt = restored.public_session()['recovery_attempts'][-1]
        assert attempt['status'] == 'unknown'
        assert attempt['node'] == 'catalog-b'
        assert 'fault_action' not in restored.public_session()
    run(scenario())


def test_reset_preserves_verified_recovery_outcome(lab):
    async def scenario():
        controller,_=lab
        await baseline(controller)
        await controller.start(Start(mode='practice',key='start-preserve'))
        await controller.mutate('recover-preserve','catalog-b','start')
        controller.session['status']='verifying'
        controller.successes=0
        for _ in range(3): await controller.cycle()
        recovered_at=controller.session['recovery_verified_at']
        recovered_ids=list(controller.session['recovery_evidence_ids'])
        await controller.reset_locked('reset-preserve')
        for _ in range(3): await controller.cycle()
        assert controller.session['status']=='ended'
        assert controller.session['outcome']=='verified_recovery'
        assert controller.session['recovery_verified_at']==recovered_at
        assert controller.session['closure']=='operator_reset'
        assert controller.session['recovery_evidence_ids'] == recovered_ids
        assert len(controller.session['reset_evidence_ids']) == 12
        assert set(recovered_ids).isdisjoint(controller.session['reset_evidence_ids'])
    run(scenario())


def test_restart_restores_committed_recovery_then_requires_fresh_verification(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-restart'))
        await controller.mutate('recovery-before-crash', 'catalog-b', 'start')
        # The broker result committed, but the API did not update the session yet.
        before = list(fleet.mutations)
        restored = Lab(controller.store, controller.client, resource_check=False)
        assert restored.session['status'] == 'verifying'
        assert restored.session['recovery_attempts'][-1]['status'] == 'succeeded'
        assert fleet.mutations == before
        for _ in range(2):
            await restored.cycle()
            assert restored.session['status'] == 'verifying'
        await restored.cycle()
        assert restored.session['status'] == 'recovered'
        assert fleet.mutations == before
    run(scenario())


def test_explicit_retry_completes_interrupted_reset_without_repeating_action(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-reset-crash'))
        s = controller.session
        s.update(status='resetting', reset_key='reset-interrupted',
                 reset={'automatic': True, 'at': time.time(), 'results': []})
        controller.store.save(s)
        await controller.mutate('reset-interrupted-gateway-a', 'gateway-a', 'start')
        before = list(fleet.mutations)
        restored = Lab(controller.store, controller.client, resource_check=False)
        assert fleet.mutations == before
        assert restored.session['recovery_attempts'] == []
        assert 'interrupted' in restored.session['verification']
        await restored.reset_locked('reset-interrupted')
        assert fleet.mutations == before + [('catalog-b', 'start')]
        for _ in range(3):
            await restored.cycle()
        assert restored.session['status'] == 'ended'
        assert restored.session['closure'] == 'automatic_reset'
        assert restored.session['ended_at'] >= s['reset']['at']
    run(scenario())


def test_future_measurement_is_not_fresh(lab):
    async def scenario():
        controller, _ = lab
        await baseline(controller)
        for item in controller.latest.values():
            item['at'] += 60
        assert all(n['health'] == 'unknown' for n in controller.state()['nodes'])
    run(scenario())


async def investigate(controller):
    for _ in range(3):
        await controller.cycle()
    assert controller.session['status'] == 'investigating'
    return {n['id']: n['health'] for n in controller.state()['nodes']}


async def verify(controller, key, node, verb):
    result = await controller.mutate(key, node, verb)
    controller.session['recovery_attempts'].append({'key': key, 'at': time.time(), **result})
    controller.session['status'] = 'verifying'
    controller.successes = 0
    for _ in range(3):
        await controller.cycle()
    return result


def test_cpu_starvation_is_slow_not_down_and_restore_recovers(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', kind='cpu-starvation', key='start-cpu'))
        assert fleet.busy['catalog-b'] and fleet.running['catalog-b']
        assert await investigate(controller) == {'gateway-a': 'degraded', 'catalog-b': 'degraded'}
        assert controller.latest[('catalog-b', 'liveness')]['body']['slow']
        evidence = await controller.check('catalog-b', 'resources')
        assert evidence['body']['cpu_pct_of_limit'] == 100.0
        wrong = await controller.mutate('wrong-cpu', 'catalog-b', 'start')
        assert wrong['status'] == 'unchanged' and 'nothing changed' in wrong['detail']
        await verify(controller, 'calm-cpu', 'catalog-b', 'calm')
        assert controller.session['status'] == 'recovered'
        assert 'runaway background job' in controller.public_session(controller.session | {'revealed': True}).get('debrief', '')
    run(scenario())


def test_disk_full_fails_writes_and_clear_recovers(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', kind='disk-full', target='gateway-a', key='start-disk'))
        assert fleet.full['gateway-a']
        assert await investigate(controller) == {'gateway-a': 'degraded', 'catalog-b': 'healthy'}
        assert controller.latest[('gateway-a', 'business')]['body']['http_status'] == 507
        disk = await controller.check('gateway-a', 'disk')
        assert disk['body']['result']['free_bytes'] == 0
        assert (await controller.mutate('wrong-clear', 'catalog-b', 'clear'))['status'] == 'unchanged'
        await verify(controller, 'clear-disk', 'gateway-a', 'clear')
        assert controller.session['status'] == 'recovered'
    run(scenario())


def test_reset_reverses_non_stop_faults(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', kind='disk-full', key='start-disk-reset'))
        await controller.reset_locked('reset-disk')
        assert not fleet.full['catalog-b']
        assert [r['verb'] for r in controller.session['reset']['results']] == ['start', 'start', 'clear']
        for _ in range(3):
            await controller.cycle()
        assert controller.session['status'] == 'ended' and controller.session['outcome'] == 'reset'
    run(scenario())


def test_challenge_hides_fault_kind_and_target(lab):
    async def scenario():
        controller, _ = lab
        seen = set()
        for attempt in range(40):
            await baseline(controller)
            await controller.start(Start(mode='challenge', key=f'challenge-kind-{attempt:03d}'))
            public = controller.state()
            assert 'scenario' not in public['session'] and 'fault_kind' not in json.dumps(public)
            seen.add(controller.session['fault_kind'])
            await controller.reset_locked(f'reset-kind-{attempt:03d}')
            for _ in range(3):
                await controller.cycle()
            if len(seen) == 4:
                break
        assert seen == {'service-unavailable', 'cpu-starvation', 'disk-full', 'config-drift'}
    run(scenario())


def test_report_counts_without_scoring(lab):
    async def scenario():
        controller, _ = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', key='start-report'))
        await investigate(controller)
        await controller.check('catalog-b', 'service')
        unchanged = await controller.mutate('ineffective', 'gateway-a', 'start')
        controller.session['recovery_attempts'].append({'key': 'ineffective', 'at': time.time(), **unchanged})
        await verify(controller, 'effective', 'catalog-b', 'start')
        report = controller.report(controller.session)
        assert report['operator_checks'] == 1 and report['checks_by_kind'] == {'service': 1}
        assert report['ineffective_attempts'] == 1 and report['effective_actions'] == 1
        assert report['detected_after_s'] is not None and report['verified_after_s'] is not None
        assert 'score' not in report
        timeline = controller.timeline(controller.session['id'])
        assert timeline['points'] and any(e['label'] == 'Recovery verified' for e in timeline['events'])
    run(scenario())


def test_disk_scenario_requires_fleet_token(lab):
    async def scenario():
        controller, fleet = lab
        controller.fleet_token = ''
        await baseline(controller)
        with pytest.raises(HTTPException) as exc:
            await controller.start(Start(mode='practice', kind='disk-full', key='start-no-token'))
        assert exc.value.status_code == 409 and not fleet.mutations
    run(scenario())


def test_config_drift_looks_like_a_dependency_failure_until_config_is_read(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', kind='config-drift', target='gateway-a', key='start-drift'))
        assert fleet.drifted['gateway-a'] and fleet.running['gateway-a']
        assert await investigate(controller) == {'gateway-a': 'degraded', 'catalog-b': 'healthy'}
        config = await controller.check('gateway-a', 'config')
        assert config['body']['result']['drifted'] == ['upstream']
        trace = await controller.check('gateway-a', 'trace')
        assert trace['body']['outside_handler_ms'] >= 0
        assert (await controller.mutate('wrong-start', 'gateway-a', 'start'))['status'] == 'unchanged'
        assert (await controller.mutate('wrong-restore', 'catalog-b', 'restore'))['status'] == 'unchanged'
        await verify(controller, 'restore-config', 'gateway-a', 'restore')
        assert controller.session['status'] == 'recovered'
        assert 'configuration reload' in controller.public_session(controller.session | {'revealed': True})['debrief']
    run(scenario())


def test_reset_restores_drifted_config(lab):
    async def scenario():
        controller, fleet = lab
        await baseline(controller)
        await controller.start(Start(mode='practice', kind='config-drift', key='start-drift-reset'))
        assert fleet.drifted['catalog-b']
        await controller.reset_locked('reset-drift')
        assert not fleet.drifted['catalog-b']
        assert [r['verb'] for r in controller.session['reset']['results']] == ['start', 'start', 'restore']
    run(scenario())
