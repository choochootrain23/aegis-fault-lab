"""The same durable workflow cannot be driven by two controller processes."""
import subprocess
import sys

import pytest

from aegis.ownership import controller_ownership


def test_lock_excludes_another_process_and_releases_after_exit(tmp_path):
    path = tmp_path/'lab.sqlite'
    program = (
        "from aegis.ownership import controller_ownership\n"
        "import sys\n"
        "with controller_ownership(sys.argv[1]):\n"
        " print('acquired')\n"
    )
    with controller_ownership(path):
        result = subprocess.run([sys.executable, '-c', program, str(path)],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode != 0
        assert 'already has an active controller' in result.stderr
    result = subprocess.run([sys.executable, '-c', program, str(path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == 'acquired'


def test_lock_releases_after_failure_and_resolves_aliases(tmp_path):
    path = tmp_path/'lab.sqlite'
    with pytest.raises(ValueError):
        with controller_ownership(path):
            with pytest.raises(RuntimeError):
                with controller_ownership(tmp_path/'nested'/ '..'/'lab.sqlite'):
                    pytest.fail('Alias bypassed ownership lock')
            raise ValueError('Startup failed')
    with controller_ownership(path):
        pass
