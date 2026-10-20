"""Private Docker boundary: no shell, arbitrary targets or generic Docker proxy."""
import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel, ConfigDict, Field

PROJECT = 'aegis-rack-diagnostics'
TARGETS = ('gateway-a', 'catalog-b')
TOKEN = os.getenv('AEGIS_BROKER_TOKEN', '')
READ_VERBS = ('inspect', 'resources')


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['gateway-a', 'catalog-b']
    verb: Literal['inspect', 'resources', 'start', 'stop']
    generation: str | None = Field(default=None, max_length=80)


def validate_target(data, target):
    labels = data.get('Config', {}).get('Labels') or {}
    host = data.get('HostConfig', {})
    networks = set(data.get('NetworkSettings', {}).get('Networks', {}))
    if labels.get('com.docker.compose.project') != PROJECT or labels.get('com.docker.compose.service') != target:
        raise ValueError('Target ownership mismatch')
    if host.get('Privileged') or host.get('NetworkMode') == 'host' or host.get('PidMode') == 'host':
        raise ValueError('Unsafe target isolation')
    if not host.get('ReadonlyRootfs') or data.get('Config', {}).get('User') != '10001:10001':
        raise ValueError('Unexpected target privilege')
    if not networks or networks != {PROJECT + '_fleet'}:
        raise ValueError('Unexpected target networks')
    if any(m.get('Type') != 'tmpfs' or m.get('Destination') != '/tmp' for m in data.get('Mounts', [])) or host.get('PortBindings'):
        raise ValueError('Unexpected target mounts or published ports')
    if not 0 < host.get('Memory', 0) <= 192 * 1024 * 1024 or not 0 < host.get('NanoCpus', 0) <= 250_000_000:
        raise ValueError('Missing resource limits')
    if host.get('PidsLimit') != 64 or host.get('CapAdd') or 'ALL' not in (host.get('CapDrop') or []):
        raise ValueError('Unexpected capability or PID limits')
    if not any(option in ('no-new-privileges', 'no-new-privileges:true') for option in (host.get('SecurityOpt') or [])):
        raise ValueError('Privilege escalation prevention missing')
    if host.get('MemorySwap') != host.get('Memory') or not 0 < host.get('ShmSize', 0) <= 8 * 1024 * 1024:
        raise ValueError('Unexpected swap or shared-memory limits')
    if host.get('RestartPolicy', {}).get('Name') not in ('no', ''):
        raise ValueError('Automatic restart would invalidate controlled-fault semantics')
    if host.get('Devices') or host.get('DeviceRequests') or host.get('Binds') or host.get('VolumesFrom'):
        raise ValueError('Unexpected device or host resource access')
    if host.get('IpcMode') not in ('private', '') or host.get('PidMode') not in ('', None):
        raise ValueError('Unexpected shared namespace')
    temporary = host.get('Tmpfs') or {}
    if set(temporary) != {'/tmp'}:
        raise ValueError('Unexpected writable temporary mounts')
    options = set(temporary['/tmp'].split(','))
    if not options.intersection({'size=8m', 'size=8388608', 'size=8192k'}):
        raise ValueError('Temporary filesystem size must be bounded to 8 MiB')


class Docker:
    def __init__(self, client):
        self.client = client

    async def resolve(self, target):
        filters = json.dumps({'label': [f'com.docker.compose.project={PROJECT}', f'com.docker.compose.service={target}']})
        result = await self.client.get('/containers/json', params={'all': 'true', 'filters': filters})
        result.raise_for_status()
        containers = result.json()
        if len(containers) != 1:
            raise ValueError('Expected exactly one owned target')
        result = await self.client.get('/containers/' + containers[0]['Id'] + '/json')
        result.raise_for_status()
        data = result.json()
        validate_target(data, target)
        return data

    async def operate(self, op):
        data = await self.resolve(op.target)
        if op.generation and data['Id'] != op.generation:
            raise ValueError('Container identity changed; inspect again')
        changed = False
        if op.verb not in READ_VERBS:
            if not op.generation:
                raise ValueError('Mutation requires inspected container identity')
            changed = await self.mutate(data, op.verb)
            data = await self.resolve(op.target)
            if data['Id'] != op.generation:
                raise ValueError('Container replaced during action; result uncertain')
        if op.verb in ('start', 'stop') and data['State']['Running'] != (op.verb == 'start'):
            raise ValueError('Observed state does not match requested action')
        result = {'target': op.target, 'generation': data['Id'], 'running': data['State']['Running'],
                  'status': data['State']['Status'], 'exit_code': data['State']['ExitCode'], 'changed': changed}
        if op.verb == 'resources':
            result.update(await self.resources(data))
        return result

    async def mutate(self, data, verb):
        """Start or stop; return False when the container is already in the requested state."""
        if data['State']['Running'] == (verb == 'start'):
            return False
        response = await self.client.post('/containers/' + data['Id'] + '/' + verb, params={'t': 3})
        response.raise_for_status()
        return True

    async def resources(self, data):
        """CPU use against the container limit, kernel throttling counters and memory, from Docker stats.

        A non-streaming sample includes the previous reading, so CPU use is a real rate over about a second.
        """
        limit = data['HostConfig']['NanoCpus'] / 1e9
        report = {'cpu_limit': limit}
        if not data['State']['Running']:
            return report
        response = await self.client.get('/containers/' + data['Id'] + '/stats', params={'stream': 'false'})
        response.raise_for_status()
        stats = response.json()
        cpu, before = stats.get('cpu_stats', {}), stats.get('precpu_stats', {})
        used = cpu.get('cpu_usage', {}).get('total_usage', 0) - before.get('cpu_usage', {}).get('total_usage', 0)
        system = cpu.get('system_cpu_usage', 0) - before.get('system_cpu_usage', 0)
        cores = used / system * cpu.get('online_cpus', 1) if system > 0 else None
        throttling = cpu.get('throttling_data', {})
        memory = stats.get('memory_stats', {})
        report.update(cpu_cores=round(cores, 3) if cores is not None else None,
                      cpu_pct_of_limit=round(100 * cores / limit, 1) if cores is not None and limit else None,
                      periods=throttling.get('periods'), throttled_periods=throttling.get('throttled_periods'),
                      throttled_ms=round(throttling.get('throttled_time', 0) / 1e6, 1),
                      memory_mib=round(memory.get('usage', 0) / 2**20, 1), memory_limit_mib=round(memory.get('limit', 0) / 2**20, 1))
        return report


@asynccontextmanager
async def lifespan(app):
    if len(TOKEN) < 32:
        raise RuntimeError('Set a broker token of at least 32 characters')
    transport = httpx.AsyncHTTPTransport(uds='/var/run/docker.sock')
    async with httpx.AsyncClient(transport=transport, base_url='http://docker/v1.45', timeout=8) as client:
        app.state.docker = Docker(client)
        app.state.lock = asyncio.Lock()
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def authorize(authorization: str = Header(default='')):
    if not TOKEN or not hmac.compare_digest(authorization, 'Bearer ' + TOKEN):
        raise HTTPException(401, 'Broker authorization required')


@app.post('/operation', dependencies=[Depends(authorize)])
async def operation(op: Operation):
    async with app.state.lock:
        try:
            return await app.state.docker.operate(op)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except httpx.HTTPError as error:
            raise HTTPException(503, 'Docker unavailable or operation failed; inspect before retrying') from error
