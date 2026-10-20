"""A controller restart may close TCP without a complete HTTP response."""
from http.client import RemoteDisconnected

import pytest

from scripts import validate_f1


@pytest.mark.parametrize('error', [RemoteDisconnected('restart'), ConnectionResetError('restart'), TimeoutError('slow response')])
def test_transport_interruption_is_reported_without_replaying_request(monkeypatch, error):
    calls = []
    def interrupted(*args, **kwargs):
        calls.append(args)
        raise error
    monkeypatch.setattr(validate_f1, 'urlopen', interrupted)
    with pytest.raises(RuntimeError, match='no completion assumed'):
        validate_f1.Operator().request('/api/state')
    assert len(calls) == 1
