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
