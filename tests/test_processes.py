"""Opt-in real-process HTTP test. This does not validate Docker isolation."""
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest


@pytest.mark.skipif(os.getenv('AEGIS_PROCESS_TESTS') != '1', reason='Opt-in: binds loopback sockets and starts two owned service processes')
def test_real_dependency_failure_and_recovery():
    sockets = [socket.socket(), socket.socket()]
    for s in sockets:
        s.bind(('127.0.0.1', 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    processes = []
    def start(role, port):
        env = {**os.environ, 'AEGIS_ROLE': role, 'AEGIS_UPSTREAM': f'http://127.0.0.1:{ports[0]}'}
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'aegis.fleet:app', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        for _ in range(60):
            try:
                if httpx.get(f'http://127.0.0.1:{port}/live', timeout=.2, trust_env=False).status_code == 200:
                    return process
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                pytest.fail('Service exited during startup')
            time.sleep(.05)
        pytest.fail('Service did not start')
    try:
        catalog = start('catalog', ports[0])
        start('gateway', ports[1])
        with httpx.Client(base_url=f'http://127.0.0.1:{ports[1]}', timeout=2, trust_env=False) as client:
            assert client.get('/summary').status_code == 200
            catalog.terminate()
            catalog.wait(timeout=5)
            assert client.get('/live').status_code == 200
            failed = client.get('/summary')
            assert failed.status_code == 503
            assert failed.json()['error'] == 'ConnectError'
            assert client.get('/diagnostics/logs').json()['entries'][-1]['ok'] is False
            start('catalog', ports[0])
            for _ in range(3):
                assert client.get('/summary').status_code == 200
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
