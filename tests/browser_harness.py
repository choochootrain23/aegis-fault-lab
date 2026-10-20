"""Test-only controller with real subprocess services and a process-control adapter.

This does not test Docker isolation or the production broker. No runtime fallback
imports this module. The harness can only control subprocesses it starts itself.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import tempfile
import time

import httpx

from aegis import controller
from aegis.ownership import controller_ownership
from aegis.store import Store


class ProcessFleet:
    def __init__(self):
        sockets = [socket.socket(), socket.socket()]
        try:
            for sock in sockets:
                sock.bind(('127.0.0.1', 0))
            self.ports = dict(zip(('catalog-b', 'gateway-a'), (s.getsockname()[1] for s in sockets)))
        finally:
            for sock in sockets:
                sock.close()
        self.processes = {}
        self.generations = {node: secrets.token_hex(16) for node in self.ports}
        self.nodes = {node: f'http://127.0.0.1:{port}' for node, port in self.ports.items()}
        self.mutations = []
        # Each service process gets its own scratch directory instead of the host's shared /tmp.
        self.scratch = {node: tempfile.mkdtemp(prefix=f'aegis-{node}-') for node in self.ports}

    def running(self, node):
        process = self.processes.get(node)
        return process is not None and process.returncode is None

    async def start(self, node):
        if self.running(node):
            return
        env = {**os.environ, 'AEGIS_ROLE': 'catalog' if node == 'catalog-b' else 'gateway',
               'AEGIS_UPSTREAM': self.nodes['catalog-b'], 'AEGIS_SCRATCH': self.scratch[node]}
        self.processes[node] = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'uvicorn', 'aegis.fleet:app', '--host', '127.0.0.1',
            '--port', str(self.ports[node]), '--no-access-log', env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(100):
                if not self.running(node):
                    raise RuntimeError('Owned test service exited at startup')
                try:
                    if (await client.get(self.nodes[node]+'/live', timeout=.2)).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.05)
        raise RuntimeError('Owned test service did not become ready')

    async def stop(self, node):
        if not self.running(node):
            return
        process = self.processes[node]
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def operation(self, request):
        op = json.loads(request.content)
        node, verb = op['target'], op['verb']
        if node not in self.nodes or verb not in ('inspect', 'start', 'stop'):
            return httpx.Response(422)
        changed = False
        if verb != 'inspect':
            if op['generation'] != self.generations[node]:
                return httpx.Response(409)
            changed = self.running(node) != (verb == 'start')
            await getattr(self, verb)(node)
            self.mutations.append({'node': node, 'verb': verb, 'at': time.time(), 'changed': changed})
        return httpx.Response(200, json={'target': node, 'generation': self.generations[node], 'changed': changed,
                                        'running': self.running(node), 'status': 'running' if self.running(node) else 'exited',
                                        'source': 'test subprocess adapter; not Docker inspection'})


@asynccontextmanager
async def lifespan(app):
    if os.getenv('AEGIS_BROWSER_TEST') != '1':
        raise RuntimeError('This test-only harness requires AEGIS_BROWSER_TEST=1')
    path = Path(os.environ['AEGIS_STATE'])
    # Service subprocesses inherit this, so maintenance-based faults work in the harness too.
    os.environ.setdefault('AEGIS_FLEET_TOKEN', secrets.token_hex(16))
    fleet = ProcessFleet()
    with controller_ownership(path):
        store = Store(path)
        try:
            for node in fleet.nodes:
                await fleet.start(node)
            # Only broker requests are adapted. All service observations use real HTTP.
            mounts = {'http://broker:8000': httpx.MockTransport(fleet.operation)}
            async with httpx.AsyncClient(mounts=mounts, trust_env=False) as client:
                lab = controller.Lab(store, client, nodes=fleet.nodes, resource_check=False)
                app.state.lab = lab
                app.state.csrf = secrets.token_urlsafe(32)
                async def collect():
                    while True:
                        await lab.cycle()
                        await asyncio.sleep(.25)
                task = asyncio.create_task(collect())
                try:
                    yield
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        finally:
            for node in fleet.nodes:
                await fleet.stop(node)
                shutil.rmtree(fleet.scratch[node], ignore_errors=True)
            (path.parent/'process-mutations.json').write_text(json.dumps(fleet.mutations))
            store.db.close()


app = controller.app
app.router.lifespan_context = lifespan


@app.post('/api/test/expire')
async def expire_for_browser_test(cleanup: bool = False):
    """Advance only the isolated test lease; service evidence remains measured."""
    lab = app.state.lab
    async with lab.lock:
        if not lab.session:
            raise RuntimeError('Test requires an active investigation')
        lab.session['deadline'] = time.time() - (61 if cleanup else 1)
        lab.store.save(lab.session)
    return {'test_lease_expired': True}
