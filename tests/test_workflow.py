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
        self.mutations = []
        self.broker_available = True

    def handle(self, request):
        if request.url.host == 'broker':
            if not self.broker_available:
                return httpx.Response(503)
            op = json.loads(request.content)
            node = op['target']
            if op['verb'] != 'inspect':
                if op['generation'] != node + '-generation':
                    return httpx.Response(409)
                self.running[node] = op['verb'] == 'start'
                self.mutations.append((node, op['verb']))
            return httpx.Response(200, json={'target': node, 'generation': node + '-generation', 'running': self.running[node],
                                          'status': 'running' if self.running[node] else 'exited', 'exit_code': 0})
        node = request.url.host
        if not self.running[node]:
            raise httpx.ConnectError('Process stopped', request=request)
        ok = request.url.path == '/live' or node == 'catalog-b' or self.running['catalog-b']
        return httpx.Response(200 if ok else 503, json={'ok': ok, 'dependency': 'catalog-b'})


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def lab(tmp_path):
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
