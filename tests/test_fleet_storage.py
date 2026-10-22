"""Service-side storage behaviour: receipts, disk reports and the bounded fill/clear hooks."""
import asyncio
import errno

import httpx
import pytest

from aegis import fleet

TOKEN = {'X-Aegis-Fleet-Token': 'fleet-token'}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, 'SCRATCH', tmp_path)
    monkeypatch.setattr(fleet, 'FLEET_TOKEN', 'fleet-token')
    fleet.LOGS.clear()
    fleet.config.update(fleet.DEPLOYED)
    fleet.quota.clear()
    fleet.quota.update(fleet.QUOTA_DEPLOYED)
    fleet.TRAFFIC.clear()
    fleet.leak_stop.set()
    fleet.leak_store.clear()
    fleet.release.update(version=fleet.STABLE, history=[{'version': fleet.STABLE, 'at': 0.0, 'by': 'deployment', 'note': 'stable build'}])

    async def call(method, path, **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fleet.app), base_url='http://catalog-b') as client:
            return await client.request(method, path, **kwargs)
    return lambda method, path, **kwargs: asyncio.run(call(method, path, **kwargs))


def test_business_requests_write_rotating_receipts(service, tmp_path):
    for _ in range(fleet.RECEIPTS + 10):
        assert service('GET', '/catalog').status_code == 200
    assert len(list((tmp_path / 'receipts').iterdir())) == fleet.RECEIPTS


def test_disk_report_measures_scratch_space(service, tmp_path):
    (tmp_path / 'big.bin').write_bytes(b'x' * 5000)
    report = service('GET', '/diagnostics/disk').json()
    assert report['total_bytes'] > 0 and report['free_bytes'] >= 0
    assert report['largest'][0] == {'path': 'big.bin', 'bytes': 5000}


def test_full_disk_turns_receipt_write_into_507(service, monkeypatch):
    def full(kind):
        raise OSError(errno.ENOSPC, 'No space left on device')
    monkeypatch.setattr(fleet, 'write_receipt', full)
    response = service('GET', '/catalog')
    assert response.status_code == 507
    assert response.json()['errno'] == 'ENOSPC'
    assert fleet.LOGS[-1]['ok'] is False and fleet.LOGS[-1]['errno'] == 'ENOSPC'


def test_maintenance_requires_token(service):
    assert service('POST', '/maintenance/fill').status_code == 403
    assert service('POST', '/maintenance/clear', headers={'X-Aegis-Fleet-Token': 'wrong'}).status_code == 403


def test_fill_refuses_unbounded_scratch_space(service, tmp_path):
    # tmp_path sits on the real disk, which is far larger than the bounded tmpfs limit.
    assert service('POST', '/maintenance/fill', headers=TOKEN).status_code == 409
    assert not (tmp_path / fleet.FILLER).exists()


def test_fill_is_capped_and_clear_removes_only_the_filler(service, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, 'BOUNDED_MOUNT', 1 << 62)
    monkeypatch.setattr(fleet, 'FILL_CAP', 1024 * 1024)
    (tmp_path / 'keep.txt').write_text('unrelated')
    filled = service('POST', '/maintenance/fill', headers=TOKEN).json()
    assert filled['changed'] and filled['written_bytes'] == 1024 * 1024
    assert (tmp_path / fleet.FILLER).stat().st_size == 1024 * 1024
    cleared = service('POST', '/maintenance/clear', headers=TOKEN).json()
    assert cleared == {'changed': True, 'freed_bytes': 1024 * 1024}
    assert (tmp_path / 'keep.txt').exists()
    assert service('POST', '/maintenance/clear', headers=TOKEN).json()['changed'] is False


def test_runaway_job_starts_once_logs_and_stops(service):
    started = service('POST', '/maintenance/burn', headers=TOKEN).json()
    try:
        assert started == {'changed': True, 'threads': fleet.JOB_THREADS}
        assert service('POST', '/maintenance/burn', headers=TOKEN).json()['changed'] is False
        assert fleet.LOGS[-1]['event'] == 'background job started'
    finally:
        stopped = service('POST', '/maintenance/calm', headers=TOKEN).json()
    assert stopped == {'changed': True, 'stopped_threads': fleet.JOB_THREADS}
    assert not any(t.is_alive() for t in fleet.job_threads)
    assert service('POST', '/maintenance/calm', headers=TOKEN).json()['changed'] is False


def test_drift_breaks_catalogue_source_and_restore_reapplies_it(service):
    assert service('GET', '/diagnostics/config').json()['drifted'] == []
    assert service('POST', '/maintenance/drift').status_code == 403
    assert service('POST', '/maintenance/drift', headers=TOKEN).json()['changed'] is True
    assert service('POST', '/maintenance/drift', headers=TOKEN).json()['changed'] is False
    report = service('GET', '/diagnostics/config').json()
    assert report['drifted'] == ['catalogue_source'] and report['settings'][0]['deployed'] == 'builtin:lab-v1'
    failed = service('GET', '/catalog')
    assert failed.status_code == 500 and failed.json()['error'] == 'FileNotFoundError'
    trace = service('GET', '/diagnostics/trace').json()
    assert trace['request_ok'] is False and trace['failed_at'] == 'catalogue load' and len(trace['spans']) == 1
    assert any(e.get('event') == 'configuration reloaded' for e in fleet.LOGS)
    assert service('POST', '/maintenance/restore', headers=TOKEN).json() == {'changed': True, 'restored': ['catalogue_source']}
    assert service('POST', '/maintenance/restore', headers=TOKEN).json()['changed'] is False
    assert service('GET', '/catalog').status_code == 200


def test_trace_times_each_step_of_a_healthy_request(service):
    trace = service('GET', '/diagnostics/trace').json()
    assert trace['request_ok'] is True and trace['failed_at'] is None
    assert [s['name'] for s in trace['spans']] == ['catalogue load', 'receipt write']
    assert all(s['ms'] >= 0 and s['start_ms'] >= 0 for s in trace['spans'])


def test_zero_quota_refuses_gateways_but_not_unlimited_callers(service):
    gateway = {'X-Aegis-Client': 'gateway-a'}
    assert service('GET', '/catalog', headers=gateway).status_code == 200
    assert service('POST', '/maintenance/squeeze').status_code == 403
    assert service('POST', '/maintenance/squeeze', headers=TOKEN).json()['changed'] is True
    assert service('POST', '/maintenance/squeeze', headers=TOKEN).json()['changed'] is False
    refused = service('GET', '/catalog', headers=gateway)
    assert refused.status_code == 429 and refused.json()['error'] == 'QuotaExceeded' and refused.json()['limit_rps'] == 0
    # The controller's probes have no quota, so the catalogue's own checks stay green.
    assert service('GET', '/summary', headers={'X-Aegis-Client': 'controller'}).status_code == 200
    report = service('GET', '/diagnostics/traffic').json()
    assert {q['client']: q['limit_rps'] for q in report['quotas']} == {'gateway-a': 0, 'gateway-c': 0}
    assert report['quota_source']['pushed_by'] == 'quota-manager'
    callers = {c['client']: c for c in report['clients']}
    assert callers['gateway-a']['requests'] == 2 and callers['gateway-a']['rejected'] == 1 and callers['controller']['rejected'] == 0
    # The quota is not part of the service's own config, so restoring config cannot fix it.
    assert service('GET', '/diagnostics/config').json()['drifted'] == []
    assert service('POST', '/maintenance/restore', headers=TOKEN).json()['changed'] is False
    assert any(e.get('event') == 'quota policy updated' for e in fleet.LOGS)
    assert service('POST', '/maintenance/requota', headers=TOKEN).json() == {'changed': True, 'restored': ['gateway-a', 'gateway-c']}
    assert service('POST', '/maintenance/requota', headers=TOKEN).json()['changed'] is False
    assert service('GET', '/catalog', headers=gateway).status_code == 200


def test_quota_limits_accepted_requests_per_second(service, monkeypatch):
    monkeypatch.setattr(fleet.time, 'time', lambda: 1000.5)  # all four requests in the same second
    monkeypatch.setitem(fleet.quota, 'gateway-c', 2)
    codes = [service('GET', '/catalog', headers={'X-Aegis-Client': 'gateway-c'}).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]


def test_network_report_reads_loss_and_retransmissions(service, monkeypatch):
    class Shown:
        returncode, stderr = 0, ''
        stdout = 'qdisc netem 8003: dev eth0 root refcnt 2 limit 1000 loss 40% seed 1277\nqdisc noqueue 0: dev lo root refcnt 2\n'
    monkeypatch.setattr(fleet.subprocess, 'run', lambda *a, **k: Shown)
    readings = iter([{'at': 10.0, 'OutSegs': 1000, 'RetransSegs': 100}, {'at': 15.0, 'OutSegs': 1400, 'RetransSegs': 200}])
    monkeypatch.setattr(fleet, 'tcp_counters', lambda: next(readings))
    monkeypatch.setattr(fleet, 'tcp_last', None)
    first = service('GET', '/diagnostics/network').json()
    assert first['qdiscs'] == [{'kind': 'netem', 'dev': 'eth0', 'root': True, 'delay_ms': None, 'loss_pct': 40.0,
                                'line': 'qdisc netem 8003: dev eth0 root refcnt 2 limit 1000 loss 40% seed 1277'}]
    assert first['tcp']['since'] == 'process start' and first['tcp']['retrans_pct'] == 10.0
    second = service('GET', '/diagnostics/network').json()['tcp']
    assert second == {'available': True, 'since': 'previous network check', 'interval_s': 5.0, 'out_segs': 400, 'retrans_segs': 100, 'retrans_pct': 25.0}


def test_bad_release_fails_business_requests_until_rolled_back(service, monkeypatch):
    assert service('POST', '/maintenance/ship').status_code == 403
    assert service('POST', '/maintenance/ship', headers=TOKEN).json() == {'changed': True, 'version': 'v1.5.0'}
    assert service('POST', '/maintenance/ship', headers=TOKEN).json()['changed'] is False
    monkeypatch.setattr(fleet.random, 'random', lambda: 0.1)
    failed = service('GET', '/catalog')
    assert failed.status_code == 500 and failed.json()['error'] == 'TypeError' and failed.json()['version'] == 'v1.5.0'
    monkeypatch.setattr(fleet.random, 'random', lambda: 0.9)
    assert service('GET', '/catalog').status_code == 200  # about half still work
    assert service('GET', '/live').json()['version'] == 'v1.5.0'
    history = service('GET', '/diagnostics/releases').json()
    assert history['running'] == 'v1.5.0' and [h['by'] for h in history['history']] == ['deployment', 'release-bot']
    # Nothing in the running config changed, so restoring it does nothing.
    assert service('POST', '/maintenance/restore', headers=TOKEN).json()['changed'] is False
    assert service('POST', '/maintenance/rollback', headers=TOKEN).json() == {'changed': True, 'from': 'v1.5.0'}
    assert service('POST', '/maintenance/rollback', headers=TOKEN).json()['changed'] is False
    monkeypatch.setattr(fleet.random, 'random', lambda: 0.1)
    assert service('GET', '/catalog').status_code == 200


def test_leaky_release_grows_memory_and_rollback_frees_it(service, monkeypatch):
    import time
    monkeypatch.setattr(fleet, 'LEAK_MIB_S', 200)
    monkeypatch.setattr(fleet, 'LEAK_CAP_MIB', 20)
    deployed = service('POST', '/maintenance/leak', headers={**TOKEN, 'X-Aegis-Actor': 'deploy-controller'}).json()
    assert deployed == {'changed': True, 'version': 'v1.5.1'}
    for _ in range(50):
        if len(fleet.leak_store) >= 20:
            break
        time.sleep(0.02)
    assert len(fleet.leak_store) == 20  # the cap holds, whatever the container limit
    assert service('GET', '/diagnostics/releases').json()['history'][-1]['by'] == 'deploy-controller'
    report = service('GET', '/diagnostics/memory').json()
    assert report['version'] == 'v1.5.1' and report['current_mib'] > 0 and 'oom_kills' in report
    assert service('POST', '/maintenance/rollback', headers=TOKEN).json()['changed'] is True
    assert fleet.leak_store == [] and fleet.leak_stop.is_set()


def test_unknown_deployers_are_recorded_as_the_release_pipeline(service):
    service('POST', '/maintenance/ship', headers={**TOKEN, 'X-Aegis-Actor': 'someone; rm -rf /'})
    assert service('GET', '/diagnostics/releases').json()['history'][-1]['by'] == 'release-bot'


def test_rate_limit_targets_heavy_unknown_callers_and_asks_them_to_back_off(service, monkeypatch):
    monkeypatch.setattr(fleet.time, 'time', lambda: 2000.5)
    for _ in range(250):
        fleet.tally('report-exporter', 200)
    for _ in range(30):
        fleet.tally('gateway-a', 200)
        fleet.tally('controller', 200)
    limited = service('POST', '/maintenance/ratelimit', headers=TOKEN).json()
    assert limited == {'changed': True, 'limited': ['report-exporter']}  # gateways and probes are never limited by it
    assert service('POST', '/maintenance/ratelimit', headers=TOKEN).json()['changed'] is False
    refused = service('GET', '/catalog', headers={'X-Aegis-Client': 'report-exporter'})
    assert refused.status_code == 429 and refused.headers['retry-after'] == '10'
    assert fleet.quota['gateway-a'] == 300 and 'controller' not in fleet.quota
    quotas = {q['client']: q for q in service('GET', '/diagnostics/traffic').json()['quotas']}
    assert quotas['report-exporter']['limit_rps'] == 2 and quotas['report-exporter']['deployed_rps'] is None
    assert service('POST', '/maintenance/requota', headers=TOKEN).json() == {'changed': True, 'restored': ['report-exporter']}
    assert 'report-exporter' not in fleet.quota


def test_only_the_reports_service_runs_the_exporter(service):
    assert service('POST', '/maintenance/flood', headers=TOKEN).json() == {'changed': False}


def test_work_whose_caller_gave_up_is_dropped_without_doing_it(service, tmp_path):
    import time
    expired = service('GET', '/catalog', headers={'X-Aegis-Deadline': f'{time.time() - 1:.3f}', 'X-Aegis-Client': 'gateway-a'})
    assert expired.status_code == 504 and expired.json()['error'] == 'DeadlineExceeded'
    assert not (tmp_path / 'receipts').exists()  # no receipt: the handler never ran
    assert service('GET', '/catalog', headers={'X-Aegis-Deadline': f'{time.time() + 5:.3f}'}).status_code == 200
    assert service('GET', '/catalog', headers={'X-Aegis-Deadline': 'not-a-time'}).status_code == 200
    traffic = {c['client']: c for c in service('GET', '/diagnostics/traffic').json()['clients']}
    assert traffic['gateway-a']['errors'] == 1
