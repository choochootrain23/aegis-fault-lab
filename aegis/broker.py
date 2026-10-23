"""Private Docker boundary: no shell, arbitrary targets or generic Docker proxy."""
import asyncio
import hmac
import ipaddress
import json
import os
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel, ConfigDict, Field

PROJECT = 'aegis-rack-diagnostics'
TARGETS = ('gateway-a', 'catalog-b', 'gateway-c')
TOKEN = os.getenv('AEGIS_BROKER_TOKEN', '')
READ_VERBS = ('inspect', 'resources')
NET_VERBS = ('delay', 'undelay', 'drop', 'undrop')
# The partition fault: on a gateway only, a prio root queue and one u32 filter that drops every packet to the catalogue's
# address. The address comes from Docker's own record of the catalogue container, never from the caller.
PARTITION_VERBS = ('isolate', 'route')
PARTITION_TARGETS = ('gateway-a', 'gateway-c')
PARTITION_PEER = 'catalog-b'
CAP_VERBS = ('cap', 'uncap')
# The power cap of the thermal fault: exactly these two CPU limits, nothing in between and nothing above the deployed one.
CPU_CAPPED, CPU_DEPLOYED = 50_000_000, 250_000_000  # 0.05 and 0.25 CPU, in Docker's NanoCpus
DELAY_MS = 600          # fixed egress delay of the network-delay fault
LOSS_PCT = 50           # fixed egress loss of the packet-loss fault; far above real-world harm (1-2%) so it shows within a minute
# Each network fault's exact netem parameters, as `tc qdisc show` prints them after the queue limit.
NETEM = {'delay': ('delay', f'{DELAY_MS}ms'), 'drop': ('loss', f'{LOSS_PCT}%')}
HELPER_IMAGE = PROJECT + ':local'
HELPER_LABEL = PROJECT + '.role'


class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: Literal['gateway-a', 'catalog-b', 'gateway-c']
    verb: Literal['inspect', 'resources', 'start', 'stop', 'delay', 'undelay', 'drop', 'undrop', 'cap', 'uncap', 'isolate', 'route']
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
            changed = await (self.network(data, op.verb) if op.verb in NET_VERBS else self.cpu_cap(data, op.verb) if op.verb in CAP_VERBS
                             else self.partition(data, op) if op.verb in PARTITION_VERBS else self.mutate(data, op.verb))
            data = await self.resolve(op.target)
            if data['Id'] != op.generation:
                raise ValueError('Container replaced during action; result uncertain')
        if op.verb in ('start', 'stop') and data['State']['Running'] != (op.verb == 'start'):
            raise ValueError('Observed state does not match requested action')
        result = {'target': op.target, 'generation': data['Id'], 'running': data['State']['Running'],
                  'status': data['State']['Status'], 'exit_code': data['State']['ExitCode'], 'changed': changed,
                  'oom_killed': bool(data['State'].get('OOMKilled')), 'started_at': data['State'].get('StartedAt'),
                  'finished_at': data['State'].get('FinishedAt')}
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

    async def cpu_cap(self, data, verb):
        """Set the container's CPU limit to the fixed capped or deployed value; False when it is already there."""
        wanted = CPU_CAPPED if verb == 'cap' else CPU_DEPLOYED
        if data['HostConfig'].get('NanoCpus') == wanted:
            return False
        response = await self.client.post('/containers/' + data['Id'] + '/update', json={'NanoCpus': wanted})
        response.raise_for_status()
        return True

    async def network(self, data, verb):
        """Add or remove one network fault's netem queue on the target's interface; False when nothing needs changing.

        Refuses to touch any queue discipline it did not create. Removing one fault's queue leaves the other
        fault's queue alone and reports nothing changed, as any other wrong remedy does.
        """
        if not data['State']['Running']:
            raise ValueError('Target must be running to change its network')
        status, shown = await self.helper(data, ['qdisc', 'show', 'dev', 'eth0'])
        if status:
            raise ValueError('Could not read the target network queue')
        root = next((line for line in shown.splitlines() if ' root ' in line), '')
        fault = 'delay' if verb in ('delay', 'undelay') else 'drop'
        settings = netem_settings(root)
        if verb in ('delay', 'drop'):
            if settings == NETEM[fault]:
                return False
            if root and not root.startswith('qdisc noqueue'):
                raise ValueError('Unexpected queue discipline present; refusing to replace it')
            argv = ['qdisc', 'add', 'dev', 'eth0', 'root', 'netem', *NETEM[fault]]
        else:
            if settings != NETEM[fault]:
                if settings is not None and settings not in NETEM.values():
                    raise ValueError('Unexpected netem settings present; refusing to remove them')
                return False
            argv = ['qdisc', 'del', 'dev', 'eth0', 'root']
        status, output = await self.helper(data, argv)
        if status:
            raise ValueError('Network change failed: ' + output[:200])
        return True

    async def peer_address(self):
        """The catalogue's IPv4 address on the fleet network, from Docker's record of the owned, validated container."""
        peer = await self.resolve(PARTITION_PEER)
        address = (peer.get('NetworkSettings', {}).get('Networks', {}).get(PROJECT + '_fleet') or {}).get('IPAddress') or ''
        try:
            parsed = ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError as error:
            raise ValueError('The catalogue has no address on the fleet network; refusing to partition') from error
        if not parsed.is_private:
            raise ValueError('Unexpected catalogue address; refusing to partition')
        return str(parsed)

    async def partition(self, data, op):
        """Add or remove the partition rule; False when nothing needs changing.

        Adding needs the default noqueue root, so it never replaces a netem fault or anything else. Removing deletes the
        root only when it is exactly this rule: a prio root whose only filter drops one destination address.
        """
        if op.verb == 'isolate' and op.target not in PARTITION_TARGETS:
            raise ValueError('Only a gateway can be partitioned from the catalogue')
        if not data['State']['Running']:
            raise ValueError('Target must be running to change its network')
        status, shown = await self.helper(data, ['qdisc', 'show', 'dev', 'eth0'])
        if status:
            raise ValueError('Could not read the target network queue')
        root = next((line for line in shown.splitlines() if ' root ' in line), '')
        ours = root.startswith('qdisc prio 1: ')
        rule = None
        if ours:
            status, filters = await self.helper(data, ['filter', 'show', 'dev', 'eth0', 'parent', '1:'])
            if status:
                raise ValueError('Could not read the target filters')
            rule = partition_rule(filters)
            if rule is None:
                raise ValueError('Unexpected filters present; refusing to touch them')
        if op.verb == 'route':
            if not ours:
                return False  # no partition here; another fault's queue is left alone
            status, output = await self.helper(data, ['qdisc', 'del', 'dev', 'eth0', 'root'])
            if status:
                raise ValueError('Network change failed: ' + output[:200])
            return True
        address = await self.peer_address()
        if ours:
            if rule == address:
                return False
            raise ValueError('A partition to another address is present; refusing to change it')
        if root and not root.startswith('qdisc noqueue'):
            raise ValueError('Unexpected queue discipline present; refusing to replace it')
        status, output = await self.helper(data, ['qdisc', 'add', 'dev', 'eth0', 'root', 'handle', '1:', 'prio'])
        if status:
            raise ValueError('Network change failed: ' + output[:200])
        status, output = await self.helper(data, ['filter', 'add', 'dev', 'eth0', 'parent', '1:', 'protocol', 'ip', 'prio', '1',
                                                  'u32', 'match', 'ip', 'dst', address + '/32', 'action', 'drop'])
        if status:
            await self.helper(data, ['qdisc', 'del', 'dev', 'eth0', 'root'])  # leave the default queue, not half a rule
            raise ValueError('Network change failed: ' + output[:200])
        return True

    async def helper(self, data, argv):
        """Run one fixed tc command inside the target's network namespace, then remove the helper.

        The helper has only NET_ADMIN, no mounts, a read-only filesystem and tight limits; it never sees the Docker socket.
        """
        config = {'Image': HELPER_IMAGE, 'Cmd': ['tc', *argv], 'User': '0:0', 'Labels': {HELPER_LABEL: 'network-helper'},
                  'HostConfig': {'NetworkMode': 'container:' + data['Id'], 'CapDrop': ['ALL'], 'CapAdd': ['NET_ADMIN'],
                                 'ReadonlyRootfs': True, 'SecurityOpt': ['no-new-privileges:true'], 'Memory': 32 * 2**20,
                                 'MemorySwap': 32 * 2**20, 'PidsLimit': 16, 'NanoCpus': 100_000_000}}
        response = await self.client.post('/containers/create', json=config)
        response.raise_for_status()
        helper = response.json()['Id']
        try:
            (await self.client.post(f'/containers/{helper}/start')).raise_for_status()
            waited = await self.client.post(f'/containers/{helper}/wait', timeout=15)
            waited.raise_for_status()
            logs = await self.client.get(f'/containers/{helper}/logs', params={'stdout': 'true', 'stderr': 'true'})
            logs.raise_for_status()
            return waited.json().get('StatusCode', 1), demultiplex(logs.content)
        finally:
            await self.client.delete(f'/containers/{helper}', params={'force': 'true'})

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


def netem_settings(root):
    """A root netem queue's parameters after its limit, without the loss seed; None for any other queue."""
    words = root.split()
    if words[:2] != ['qdisc', 'netem']:
        return None
    params = words[words.index('limit') + 2:] if 'limit' in words else words[words.index('root') + 3:]
    if 'seed' in params:
        index = params.index('seed')
        params = params[:index] + params[index + 2:]
    return tuple(params)


def partition_rule(shown):
    """The one address the partition filter drops, from `tc filter show`; None for no filter or any other filter."""
    matches = [line.split() for line in shown.splitlines() if line.strip().startswith('match ')]
    actions = [line for line in shown.splitlines() if 'action' in line and 'gact' in line]
    if len(matches) != 1 or len(actions) != 1 or not actions[0].rstrip().endswith('action drop'):
        return None
    words = matches[0]
    if len(words) != 4 or words[2] != 'at' or words[3] != '16' or not words[1].endswith('/ffffffff'):
        return None  # offset 16 of an IPv4 header is the destination address
    try:
        return str(ipaddress.IPv4Address(int(words[1].split('/')[0], 16)))
    except ValueError:
        return None


def demultiplex(raw):
    """Text from Docker's multiplexed log stream (8-byte frame headers, as used without a TTY)."""
    text, index = [], 0
    while index + 8 <= len(raw):
        size = int.from_bytes(raw[index + 4:index + 8], 'big')
        text.append(raw[index + 8:index + 8 + size].decode(errors='replace'))
        index += 8 + size
    return ''.join(text)


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
