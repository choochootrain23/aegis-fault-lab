"""Request signing between the gateways and the catalogue, and the gateway clock it depends on."""
import time

import pytest

from aegis import fleet
from test_fleet_storage import TOKEN, service  # noqa: F401  (the shared service fixture)


@pytest.fixture
def signing(service, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fleet, 'SIGNING_KEY', 'signing-key')
    fleet.clock.update(offset_s=0.0, source='host kernel clock', stepped_at=None)
    yield
    fleet.clock.update(offset_s=0.0, source='host kernel clock', stepped_at=None)


def test_the_catalogue_requires_a_valid_fresh_signature_from_a_gateway_identity(service, signing, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fleet, 'NAME', 'gateway-a')
    good = {'X-Aegis-Client': 'gateway-a', **fleet.signed_headers('GET', '/catalog')}
    assert service('GET', '/catalog', headers=good).status_code == 200
    assert service('GET', '/catalog', headers={'X-Aegis-Client': 'gateway-a'}).json()['error'] == 'SignatureMissing'
    assert service('GET', '/catalog', headers={**good, 'X-Aegis-Signature': '0' * 64}).json()['error'] == 'SignatureInvalid'
    assert service('GET', '/catalog', headers={**good, 'X-Aegis-Client': 'gateway-c'}).json()['error'] == 'SignatureInvalid'  # bound to the caller
    assert service('GET', '/catalog', headers={'X-Aegis-Client': 'controller'}).status_code == 200  # probes and batch jobs are unsigned callers
    fleet.clock['offset_s'] = 300.0  # the gateway's clock, when it signs; the catalogue checks against its own
    ahead = fleet.signed_headers('GET', '/catalog')
    fleet.clock['offset_s'] = 0.0
    expired = service('GET', '/catalog', headers={'X-Aegis-Client': 'gateway-a', **ahead})
    assert expired.status_code == 401 and expired.json()['error'] == 'RequestExpired' and 299 < expired.json()['skew_s'] < 301
    traffic = service('GET', '/diagnostics/traffic').json()
    assert traffic['signing']['enabled'] and next(c for c in traffic['clients'] if c['client'] == 'gateway-a')['rejected'] == 3  # missing, forged and expired; the one claiming gateway-c counts there
    assert any(e.get('error') == 'RequestExpired' for e in fleet.LOGS)


def test_without_a_key_signing_is_off_and_says_so(service):  # noqa: F811
    assert service('GET', '/catalog', headers={'X-Aegis-Client': 'gateway-a'}).status_code == 200
    assert service('GET', '/diagnostics/traffic').json()['signing']['enabled'] is False
    assert fleet.signed_headers('GET', '/catalog') == {}


def test_skew_steps_only_a_signing_gateway_clock_and_resync_restores_it(service, signing, monkeypatch):  # noqa: F811
    assert service('POST', '/maintenance/skew', headers=TOKEN).status_code == 409  # the catalogue has no signing clock to skew
    monkeypatch.setattr(fleet, 'ROLE', 'gateway')
    monkeypatch.setitem(fleet.config, 'upstream', 'http://127.0.0.1:9')
    assert service('POST', '/maintenance/skew', headers=TOKEN).json() == {'changed': True, 'offset_s': 300.0}
    assert service('POST', '/maintenance/skew', headers=TOKEN).json()['changed'] is False
    clock = service('GET', '/diagnostics/network').json()['clock']
    assert 299 < clock['now'] - time.time() < 301 and 'falseticker' in clock['source']
    assert fleet.LOGS[-1]['event'] == 'clock stepped' and fleet.LOGS[-1]['at'] > time.time() + 290  # its own log is in the future
    assert service('POST', '/maintenance/resync', headers=TOKEN).json() == {'changed': True, 'offset_s': 0.0}
    assert service('POST', '/maintenance/resync', headers=TOKEN).json()['changed'] is False
    assert abs(service('GET', '/diagnostics/network').json()['clock']['now'] - time.time()) < 1


def test_a_skewed_clock_still_measures_deadlines_on_the_kernel_clock(service, signing):  # noqa: F811
    fleet.clock['offset_s'] = 300.0
    try:
        fresh = service('GET', '/catalog', headers={'X-Aegis-Deadline': f'{time.time() + 5:.3f}'})
        assert fresh.status_code == 200  # a 5-second budget from the caller is not mistaken for one that ran out 295 s ago
    finally:
        fleet.clock['offset_s'] = 0.0
