"""Real synthetic services. Each instance is an independent Linux process."""
import asyncio
import errno
import hmac
import os
import json
import random
import re
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from collections import deque
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

ROLE = os.getenv('AEGIS_ROLE', 'catalog')
NAME = os.getenv('AEGIS_NAME', ROLE)  # how this service identifies itself to the services it calls
UPSTREAM = os.getenv('AEGIS_UPSTREAM', 'http://catalog-b:8000')
SCRATCH = Path(os.getenv('AEGIS_SCRATCH', '/tmp'))
FLEET_TOKEN = os.getenv('AEGIS_FLEET_TOKEN', '')
FILLER = 'aegis-fill.bin'
RECEIPTS = 40              # ring of small receipt files written by every business request
FILL_CAP = 16 * 1024**2    # never write more than this, whatever the mount reports
BOUNDED_MOUNT = 64 * 1024**2  # only fill scratch space that is a small bounded tmpfs
JOB_THREADS = 3            # runaway job workers; the container CPU limit still caps them
JOB_MAX_S = 1500           # a forgotten job stops itself after 25 minutes
job_stop = threading.Event()
job_threads = []
BOOT = str(uuid.uuid4())
LOGS = deque(maxlen=200)
receipt_counter = 0
# Runtime settings. The deployed values come from the image and environment; a drift fault overrides
# one in memory, as a bad hot reload would, and restore reapplies the deployed values.
DEPLOYED = {'gateway': {'upstream': UPSTREAM, 'upstream_timeout_s': 1.0},
            'catalog': {'catalogue_source': 'builtin:lab-v1', 'receipt_ring': RECEIPTS}}.get(ROLE, {})
DRIFT = {'gateway': ('upstream', UPSTREAM.rsplit(':', 1)[0] + ':8080'),
         'catalog': ('catalogue_source', 'file:' + str(SCRATCH / 'catalogue-v2.json'))}.get(ROLE)
config = dict(DEPLOYED)
# Per-caller request quotas, enforced by the catalogue on its business endpoint. They come from a separate quota
# system rather than the service's own config, so a bad quota push leaves the running config untouched. Callers
# without a quota, such as the controller's health probes, are not limited.
QUOTA_DEPLOYED = {'gateway-a': 20, 'gateway-c': 20} if ROLE == 'catalog' else {}
quota = dict(QUOTA_DEPLOYED)
quota_source = {'pushed_by': 'deployment', 'at': time.time()}
TRAFFIC_WINDOW_S = 60
# Business requests per caller in one-second buckets, newest last: [second, {client: [requests, rejected, errors, accepted]}].
# Buckets rather than single requests, so a flood of hundreds a second cannot push everyone else out of the window.
TRAFFIC = deque(maxlen=TRAFFIC_WINDOW_S + 1)
HEAVY_RPS = 20        # a caller without a deployed quota above this is limited by the rate-limit remedy
RECENT_S = 10         # the window for a caller's current rate
RATE_LIMIT_RPS = 2    # what it is limited to
RETRY_AFTER_S = 10    # how long a limited caller is asked to wait
tcp_last = None  # TCP counters at the previous network check, so retransmissions are reported per interval
# Releases. Every process boots on the stable build; the release faults deploy a newer one in place, as a rollout
# would, and rollback returns to the stable build. Only these fixed versions exist.
STABLE = 'v1.4.2'
RELEASE_NOTES = {STABLE: 'stable build', 'v1.5.0': 'faster catalogue lookups', 'v1.5.1': 'warm the item cache in the background'}
BAD_RELEASE = 'v1.5.0'       # a code path that fails about half of business requests
LEAKY_RELEASE = 'v1.5.1'     # a cache warmer that never frees what it loads
BAD_RELEASE_FAILURE = 0.5
LEAK_MIB_S = 12              # how fast the leaky build grows; the container's 192 MiB limit stops it within seconds
LEAK_CAP_MIB = 512           # never hold more than this, whatever the limit
release = {'version': STABLE, 'history': [{'version': STABLE, 'at': time.time(), 'by': 'deployment', 'note': RELEASE_NOTES[STABLE]}]}
leak_store = []
leak_stop = threading.Event()
MEMORY = deque(maxlen=31)    # (at, bytes) sampled every 2 seconds from this container's cgroup


def cgroup_memory():
    """This container's memory use, limit and OOM-kill count, from its own cgroup (read-only)."""
    root = Path('/sys/fs/cgroup')
    try:
        current = int((root / 'memory.current').read_text())
        limit = (root / 'memory.max').read_text().strip()
        events = dict(line.split() for line in (root / 'memory.events').read_text().splitlines())
        return current, None if limit == 'max' else int(limit), int(events.get('oom_kill', 0))
    except (OSError, ValueError):
        rss = next((int(line.split()[1]) * 1024 for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:')), 0)
        return rss, None, None


def sample_memory():
    while True:
        MEMORY.append((time.time(), cgroup_memory()[0]))
        time.sleep(2)


@asynccontextmanager
async def lifespan(app):
    threading.Thread(target=sample_memory, name='memory-sampler', daemon=True).start()
    async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


class CountTraffic:
    """Counts business requests per caller from the response status. A plain ASGI middleware on purpose: Starlette's
    decorator-style middleware cancels the handler when a caller disconnects, which left the gateways' pooled
    connections to the catalogue marked in use for good (measured: a retry storm exhausted the pool permanently)."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] not in ('/summary', '/catalog', '/ready'):
            return await self.app(scope, receive, send)
        client = dict(scope['headers']).get(b'x-aegis-client', b'unidentified').decode('latin-1')[:40]

        async def counted(message):
            if message['type'] == 'http.response.start':
                tally(client, message['status'])
            await send(message)
        await self.app(scope, receive, counted)


app.add_middleware(CountTraffic)


def tally(client, status):
    second = int(time.time())
    if not TRAFFIC or TRAFFIC[-1][0] != second:
        TRAFFIC.append([second, {}])
    counts = TRAFFIC[-1][1].setdefault(client, [0, 0, 0, 0])
    counts[0] += 1
    counts[1] += status == 429
    counts[2] += status >= 500
    counts[3] += status < 400


def write_receipt(kind):
    """Record a small receipt on the scratch filesystem; this is the real write a full disk breaks.

    Each receipt is a new file and the oldest is removed only afterwards: rewriting an existing
    tmpfs file can reuse its page, which would hide a full filesystem.
    """
    global receipt_counter
    receipt_counter += 1
    folder = SCRATCH / 'receipts'
    folder.mkdir(exist_ok=True)
    (folder / f'{BOOT[:8]}-{receipt_counter:08d}.json').write_text(json.dumps({'at': time.time(), 'kind': kind, 'generation': BOOT, 'pad': 'x' * 160}))
    stale = sorted(folder.iterdir())[:-config.get('receipt_ring', RECEIPTS)]
    for path in stale:
        path.unlink(missing_ok=True)


def storage_failure(error, started):
    code = errno.errorcode.get(error.errno, 'unknown') if error.errno else 'unknown'
    result = {'ok': False, 'error': 'OSError', 'errno': code, 'detail': os.strerror(error.errno) if error.errno else str(error),
              'path': 'receipt write', 'generation': BOOT, 'duration_ms': round((time.monotonic() - started) * 1000, 1)}
    LOGS.append({'at': time.time(), **result})
    return JSONResponse(result, status_code=507)


@app.get('/live')
async def live():
    return {'live': True, 'role': ROLE, 'generation': BOOT, 'version': release['version']}


def release_failure(started):
    """The bad release's broken code path: about half of business requests fail inside the service."""
    if release['version'] != BAD_RELEASE or random.random() >= BAD_RELEASE_FAILURE:
        return None
    result = {'ok': False, 'error': 'TypeError', 'detail': "unsupported operand type(s) for *: 'NoneType' and 'float'", 'path': 'price conversion',
              'version': release['version'], 'generation': BOOT, 'duration_ms': round((time.monotonic() - started) * 1000, 1)}
    LOGS.append({'at': time.time(), **result})
    return JSONResponse(result, status_code=500)


def load_catalogue():
    """Items from the configured source. Only the built-in dataset is deployed, so any file source is missing."""
    source = config.get('catalogue_source', 'builtin:lab-v1')
    if source.startswith('file:'):
        return json.loads(Path(source[5:]).read_text())['items']
    return [{'sku': 'LAB-001', 'name': 'Synthetic component', 'quantity': 12}]


def over_quota(client):
    """True when this caller has already used its quota of accepted requests in the current second."""
    limit = quota.get(client)
    if limit is None:
        return False
    return bool(TRAFFIC) and TRAFFIC[-1][0] == int(time.time()) and TRAFFIC[-1][1].get(client, [0, 0, 0, 0])[3] >= limit


@app.get('/catalog')
async def catalog(x_aegis_client: str = Header(default='unidentified')):
    started = time.monotonic()
    if over_quota(x_aegis_client):
        result = {'ok': False, 'error': 'QuotaExceeded', 'client': x_aegis_client[:40], 'limit_rps': quota[x_aegis_client],
                  'detail': 'per-client request quota exhausted', 'generation': BOOT, 'duration_ms': round((time.monotonic() - started) * 1000, 1)}
        LOGS.append({'at': time.time(), **result})
        return JSONResponse(result, status_code=429, headers={'Retry-After': str(RETRY_AFTER_S)})
    if (failure := release_failure(started)):
        return failure
    try:
        items = load_catalogue()
    except (OSError, ValueError, KeyError) as error:
        result = {'ok': False, 'error': type(error).__name__, 'detail': 'catalogue source unavailable', 'source': config.get('catalogue_source'),
                  'generation': BOOT, 'duration_ms': round((time.monotonic() - started) * 1000, 1)}
        LOGS.append({'at': time.time(), **result})
        return JSONResponse(result, status_code=500)
    try:
        write_receipt('catalog')
    except OSError as error:
        return storage_failure(error, started)
    return {'items': items, 'generation': BOOT}


async def dependency():
    started = time.monotonic()
    if (failure := release_failure(started)):
        return failure
    correlation = str(uuid.uuid4())
    try:
        response = await app.state.client.get(config['upstream'] + '/catalog', headers={'X-Correlation-ID': correlation, 'X-Aegis-Client': NAME},
                                              timeout=config['upstream_timeout_s'])
        response.raise_for_status()
        result = {'ok': True, 'dependency': 'catalog-b', 'correlation': correlation, 'catalog': response.json()}
        status = 200
    except httpx.HTTPStatusError as error:
        result = {'ok': False, 'dependency': 'catalog-b', 'correlation': correlation, 'error': 'HTTPStatusError',
                  'upstream_status': error.response.status_code, 'upstream_error': upstream_error(error.response)}
        status = 503
    except (httpx.HTTPError, ValueError) as error:
        result = {'ok': False, 'dependency': 'catalog-b', 'correlation': correlation, 'error': type(error).__name__}
        status = 503
    result['generation'] = BOOT
    result['duration_ms'] = round((time.monotonic() - started) * 1000, 1)
    LOGS.append({'at': time.time(), **{k: v for k, v in result.items() if k != 'catalog'}})
    if status == 200:
        try:
            write_receipt('gateway')
        except OSError as error:
            return storage_failure(error, started)
    return JSONResponse(result, status_code=status)


def upstream_error(response):
    try:
        return str(response.json().get('error'))[:40]
    except (ValueError, AttributeError):
        return None


@app.get('/ready')
async def ready():
    if ROLE == 'gateway':
        return await dependency()
    return {'ok': True, 'generation': BOOT}


@app.get('/summary')
async def summary(x_aegis_client: str = Header(default='unidentified')):
    if ROLE == 'gateway':
        return await dependency()
    return await catalog(x_aegis_client)


@app.get('/diagnostics/logs')
async def logs():
    entries = list(LOGS)
    total = len(entries)
    while entries and len(json.dumps(entries).encode()) > 28 * 1024:
        entries.pop(0)
    return {'entries': entries, 'bounded_to': 200, 'generation': BOOT,
            'truncated': len(entries) < total, 'omitted_retained_entries': total - len(entries)}


@app.get('/diagnostics/disk')
async def disk():
    stats = os.statvfs(SCRATCH)
    total, free = stats.f_blocks * stats.f_frsize, stats.f_bavail * stats.f_frsize
    files = sorted((p for p in SCRATCH.rglob('*') if p.is_file()), key=lambda p: p.stat().st_size, reverse=True)
    return {'mount': str(SCRATCH), 'total_bytes': total, 'free_bytes': free, 'used_pct': round(100 * (total - free) / total, 1) if total else None,
            'largest': [{'path': str(p.relative_to(SCRATCH)), 'bytes': p.stat().st_size} for p in files[:5]], 'file_count': len(files), 'generation': BOOT}


@app.get('/diagnostics/config')
async def running_config():
    settings = [{'key': key, 'value': config[key], 'deployed': value, 'drifted': config[key] != value} for key, value in DEPLOYED.items()]
    return {'role': ROLE, 'settings': settings, 'drifted': [s['key'] for s in settings if s['drifted']], 'generation': BOOT}


def read_qdiscs():
    """Queue disciplines on this service's own interfaces, as `tc qdisc show` reports them (read-only)."""
    try:
        shown = subprocess.run(['tc', 'qdisc', 'show'], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'available': False, 'detail': type(error).__name__}
    if shown.returncode:
        return {'available': False, 'detail': shown.stderr.strip()[:200]}
    entries = []
    for line in shown.stdout.splitlines():
        match = re.match(r'qdisc (\S+) \S+ dev (\S+) (root|parent \S+)', line)
        if not match or match[2] == 'lo':
            continue
        delay = re.search(r'delay (\d+(?:\.\d+)?)(us|ms|s)\b', line)
        delay_ms = None if not delay else float(delay[1]) * {'us': 0.001, 'ms': 1, 's': 1000}[delay[2]]
        loss = re.search(r'loss (\d+(?:\.\d+)?)%', line)
        entries.append({'kind': match[1], 'dev': match[2], 'root': match[3] == 'root', 'delay_ms': delay_ms,
                        'loss_pct': float(loss[1]) if loss else None, 'line': line.strip()})
    return {'available': True, 'qdiscs': entries}


def tcp_counters():
    """This network namespace's TCP segment counters from /proc/net/snmp (read-only)."""
    rows = [line.split() for line in Path('/proc/net/snmp').read_text().splitlines() if line.startswith('Tcp:')]
    return {'at': time.time(), **dict(zip(rows[0][1:], map(int, rows[1][1:])))}


def retransmissions():
    """Segments sent and retransmitted since the previous network check: lost packets must be sent again."""
    global tcp_last
    try:
        now = tcp_counters()
    except (OSError, ValueError, IndexError):
        return {'available': False}
    before, tcp_last = tcp_last, now
    if not before:
        return {'available': True, 'since': 'process start', 'out_segs': now['OutSegs'], 'retrans_segs': now['RetransSegs'],
                'retrans_pct': round(100 * now['RetransSegs'] / now['OutSegs'], 1) if now['OutSegs'] else 0.0}
    sent, again = now['OutSegs'] - before['OutSegs'], now['RetransSegs'] - before['RetransSegs']
    return {'available': True, 'since': 'previous network check', 'interval_s': round(now['at'] - before['at'], 1),
            'out_segs': sent, 'retrans_segs': again, 'retrans_pct': round(100 * again / sent, 1) if sent else 0.0}


@app.get('/diagnostics/network')
async def network():
    return {**read_qdiscs(), 'tcp': retransmissions(), 'generation': BOOT}


@app.get('/diagnostics/releases')
async def releases():
    """The running version and this process's deployment history, newest last."""
    return {'running': release['version'], 'note': RELEASE_NOTES[release['version']], 'history': release['history'][-10:],
            'uptime_s': round(time.time() - release['history'][0]['at'], 1), 'generation': BOOT}


@app.get('/diagnostics/memory')
async def memory():
    """Memory against the container limit, the last minute of samples and how fast it is growing."""
    current, limit, oom_kills = cgroup_memory()
    now = time.time()
    samples = list(MEMORY) + [(now, current)]
    recent = [(at, used) for at, used in samples if now - at <= 20]
    span = recent[-1][0] - recent[0][0] if len(recent) > 1 else 0
    growth = (recent[-1][1] - recent[0][1]) / span / 2**20 if span >= 4 else None
    return {'current_mib': round(current / 2**20, 1), 'limit_mib': round(limit / 2**20, 1) if limit else None,
            'used_pct': round(100 * current / limit, 1) if limit else None, 'growth_mib_s': round(growth, 2) if growth is not None else None,
            'samples': [{'age_s': round(now - at, 1), 'mib': round(used / 2**20, 1)} for at, used in samples[-30:]],
            'oom_kills': oom_kills, 'uptime_s': round(now - release['history'][0]['at'], 1), 'version': release['version'], 'generation': BOOT}


def traffic_since(start):
    return [(second, counts) for second, counts in TRAFFIC if second >= int(start)]


@app.get('/diagnostics/traffic')
async def traffic():
    """Business requests per caller over the last minute, and the quotas this service enforces."""
    callers = {}
    for second, counts in traffic_since(time.time() - TRAFFIC_WINDOW_S):
        for client, (requests, rejected, errors, _) in counts.items():
            entry = callers.setdefault(client, {'client': client, 'requests': 0, 'rejected': 0, 'errors': 0})
            entry['requests'] += requests
            entry['rejected'] += rejected
            entry['errors'] += errors
    recent = {}
    for second, counts in traffic_since(time.time() - RECENT_S):
        for client, (requests, *_) in counts.items():
            recent[client] = recent.get(client, 0) + requests
    for entry in callers.values():
        entry['rps'] = round(entry['requests'] / TRAFFIC_WINDOW_S, 2)
        entry['recent_rps'] = round(recent.get(entry['client'], 0) / RECENT_S, 2)  # what it is sending now, not a minute ago
    quotas = [{'client': client, 'limit_rps': limit, 'deployed_rps': QUOTA_DEPLOYED.get(client), 'changed': limit != QUOTA_DEPLOYED.get(client)}
              for client, limit in quota.items()]
    return {'window_s': TRAFFIC_WINDOW_S, 'clients': sorted(callers.values(), key=lambda e: -e['requests']), 'quotas': quotas,
            'quota_source': quota_source if QUOTA_DEPLOYED else None, 'generation': BOOT}


@app.get('/diagnostics/trace')
async def trace():
    """One traced business request: how long each step took and where it failed."""
    started = time.monotonic()
    spans = []

    def span(name, began, ok, **detail):
        spans.append({'name': name, 'start_ms': round((began - started) * 1000, 1), 'ms': round((time.monotonic() - began) * 1000, 1), 'ok': ok, **detail})
        return ok

    began = time.monotonic()
    if ROLE == 'gateway':
        name = 'upstream ' + config['upstream'].split('//')[-1] + '/catalog'
        try:
            response = await app.state.client.get(config['upstream'] + '/catalog', headers={'X-Aegis-Client': NAME}, timeout=config['upstream_timeout_s'])
            ok = span(name, began, response.is_success, status=response.status_code)
        except httpx.HTTPError as error:
            ok = span(name, began, False, error=type(error).__name__)
    else:
        try:
            load_catalogue()
            ok = span('catalogue load', began, True, source=config['catalogue_source'])
        except (OSError, ValueError, KeyError) as error:
            ok = span('catalogue load', began, False, source=config['catalogue_source'], error=type(error).__name__)
    if ok:
        began = time.monotonic()
        try:
            write_receipt('trace')
            ok = span('receipt write', began, True)
        except OSError as error:
            ok = span('receipt write', began, False, error=errno.errorcode.get(error.errno, 'OSError'))
    return {'request_ok': ok, 'failed_at': next((s['name'] for s in spans if not s['ok']), None), 'spans': spans,
            'handler_ms': round((time.monotonic() - started) * 1000, 1), 'generation': BOOT}


def authorize(token):
    if not FLEET_TOKEN or not hmac.compare_digest(token, FLEET_TOKEN):
        raise HTTPException(403, 'Maintenance token required')


@app.post('/maintenance/fill')
async def fill(x_aegis_fleet_token: str = Header(default='')):
    """Fault injection: fill the bounded scratch tmpfs. Refuses anything that is not a small bounded mount."""
    authorize(x_aegis_fleet_token)
    stats = os.statvfs(SCRATCH)
    if stats.f_blocks * stats.f_frsize > BOUNDED_MOUNT:
        raise HTTPException(409, 'Refusing to fill: scratch space is not a small bounded tmpfs')
    written, chunk = 0, b'\0' * 262144
    try:
        with open(SCRATCH / FILLER, 'ab') as handle:
            while written < FILL_CAP:
                handle.write(chunk)
                handle.flush()
                written += len(chunk)
    except OSError as error:
        if error.errno != errno.ENOSPC:
            raise
    after = os.statvfs(SCRATCH)
    return {'changed': True, 'written_bytes': written, 'free_bytes': after.f_bavail * after.f_frsize}


@app.post('/maintenance/clear')
async def clear(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: remove only the known filler file."""
    authorize(x_aegis_fleet_token)
    path = SCRATCH / FILLER
    if not path.exists():
        return {'changed': False, 'freed_bytes': 0}
    size = path.stat().st_size
    path.unlink()
    return {'changed': True, 'freed_bytes': size}


def busy(deadline):
    while not job_stop.is_set() and time.monotonic() < deadline:
        sum(i * i for i in range(2000))


@app.post('/maintenance/burn')
async def burn(x_aegis_fleet_token: str = Header(default='')):
    """Fault injection: start a bounded runaway background job that competes with requests for CPU."""
    authorize(x_aegis_fleet_token)
    if any(t.is_alive() for t in job_threads):
        return {'changed': False, 'threads': len(job_threads)}
    job_stop.clear()
    job_threads.clear()
    deadline = time.monotonic() + JOB_MAX_S
    for index in range(JOB_THREADS):
        worker = threading.Thread(target=busy, args=(deadline,), name=f'reindex-{index}', daemon=True)
        worker.start()
        job_threads.append(worker)
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'background job started', 'job': 'catalogue reindex', 'threads': JOB_THREADS, 'generation': BOOT})
    return {'changed': True, 'threads': JOB_THREADS}


@app.post('/maintenance/calm')
async def calm(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: stop the runaway job."""
    authorize(x_aegis_fleet_token)
    running = [t for t in job_threads if t.is_alive()]
    job_stop.set()
    for worker in running:
        worker.join(timeout=2)
    if running:
        LOGS.append({'at': time.time(), 'ok': True, 'event': 'background job stopped', 'job': 'catalogue reindex', 'generation': BOOT})
    return {'changed': bool(running), 'stopped_threads': len(running)}


@app.post('/maintenance/drift')
async def drift(x_aegis_fleet_token: str = Header(default='')):
    """Fault injection: override one runtime setting, as a bad hot reload would."""
    authorize(x_aegis_fleet_token)
    key, value = DRIFT
    if config[key] == value:
        return {'changed': False}
    config[key] = value
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'configuration reloaded', 'changed': {key: value}, 'generation': BOOT})
    return {'changed': True, 'key': key}


@app.post('/maintenance/squeeze')
async def squeeze(x_aegis_fleet_token: str = Header(default='')):
    """Fault injection: a quota push that sets every gateway's catalogue quota to zero. Only the catalogue has quotas."""
    global quota_source
    authorize(x_aegis_fleet_token)
    if not QUOTA_DEPLOYED or all(limit == 0 for limit in quota.values()):
        return {'changed': False}
    quota.update({client: 0 for client in QUOTA_DEPLOYED})
    quota_source = {'pushed_by': 'quota-manager', 'at': time.time()}
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'quota policy updated', 'pushed_by': 'quota-manager', 'limits_rps': dict(quota), 'generation': BOOT})
    return {'changed': True}


@app.post('/maintenance/ratelimit')
async def ratelimit(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: limit every heavy caller that has no deployed quota, and ask it to back off. Only the catalogue limits callers."""
    authorize(x_aegis_fleet_token)
    if ROLE != 'catalog':
        return {'changed': False}
    recent = {}
    for second, counts in traffic_since(time.time() - RECENT_S):
        for client, (requests, *_) in counts.items():
            recent[client] = recent.get(client, 0) + requests
    heavy = sorted(client for client, requests in recent.items()
                   if requests / RECENT_S > HEAVY_RPS and client not in QUOTA_DEPLOYED and client != 'controller' and client not in quota)
    for client in heavy:
        quota[client] = RATE_LIMIT_RPS
    if heavy:
        LOGS.append({'at': time.time(), 'ok': True, 'event': 'rate limit applied', 'clients': heavy, 'limit_rps': RATE_LIMIT_RPS, 'generation': BOOT})
    return {'changed': bool(heavy), 'limited': heavy}


@app.post('/maintenance/requota')
async def requota(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: reapply the deployed per-caller quotas."""
    global quota_source
    authorize(x_aegis_fleet_token)
    changed = sorted({client for client, limit in QUOTA_DEPLOYED.items() if quota.get(client) != limit} | (set(quota) - set(QUOTA_DEPLOYED)))
    if not changed:
        return {'changed': False}
    quota.clear()
    quota.update(QUOTA_DEPLOYED)
    quota_source = {'pushed_by': 'deployment', 'at': time.time()}
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'quota policy restored', 'clients': changed, 'generation': BOOT})
    return {'changed': True, 'restored': changed}


def deploy(version, actor):
    previous = release['version']
    if previous == version:
        return False
    release['version'] = version
    release['history'].append({'version': version, 'at': time.time(), 'by': actor, 'note': RELEASE_NOTES[version]})
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'release deployed', 'version': version, 'previous': previous, 'by': actor, 'generation': BOOT})
    return True


def actor(header):
    """Who deployed: the release pipeline's first rollout, or the deploy controller re-applying it after a restart."""
    return header if header in ('release-bot', 'deploy-controller') else 'release-bot'


def grow(deadline):
    chunk = b'\x5a' * 2**20  # filled bytes, so every page is really used
    while not leak_stop.is_set() and time.monotonic() < deadline and len(leak_store) < LEAK_CAP_MIB:
        leak_store.append(bytearray(chunk))  # a fresh copy each time: real, resident memory
        time.sleep(1 / LEAK_MIB_S)


@app.post('/maintenance/ship')
async def ship(x_aegis_fleet_token: str = Header(default=''), x_aegis_actor: str = Header(default='')):
    """Fault injection: deploy the release with a broken code path."""
    authorize(x_aegis_fleet_token)
    return {'changed': deploy(BAD_RELEASE, actor(x_aegis_actor)), 'version': release['version']}


@app.post('/maintenance/leak')
async def leak(x_aegis_fleet_token: str = Header(default=''), x_aegis_actor: str = Header(default='')):
    """Fault injection: deploy the release whose cache warmer leaks until the container's memory limit stops it."""
    authorize(x_aegis_fleet_token)
    if not deploy(LEAKY_RELEASE, actor(x_aegis_actor)):
        return {'changed': False, 'version': release['version']}
    leak_stop.clear()
    threading.Thread(target=grow, args=(time.monotonic() + JOB_MAX_S,), name='cache-warmer', daemon=True).start()
    return {'changed': True, 'version': release['version']}


@app.post('/maintenance/rollback')
async def rollback(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: return to the stable build, stopping and freeing anything the newer one held."""
    authorize(x_aegis_fleet_token)
    leak_stop.set()
    leak_store.clear()
    previous = release['version']
    return {'changed': deploy(STABLE, 'rollback'), 'from': previous}


# The report exporter: a batch client that, when started, pulls the catalogue as fast as it can over many keep-alive
# connections, as a misconfigured export job would. It only ever calls the catalogue on the internal network, stops
# itself after JOB_MAX_S, and honours Retry-After like a well-behaved client, so rate-limiting it works.
FLOOD_CONNECTIONS = 160
FLOOD_REQUEST = b'GET /catalog HTTP/1.1\r\nHost: catalog-b\r\nX-Aegis-Client: report-exporter\r\n\r\n'
flood_stop = threading.Event()
flood_threads = []
flood_stats = {'sent': 0, 'rejected': 0, 'errors': 0}


async def pause(seconds):
    """Sleep, but wake within a quarter of a second when the job is told to stop."""
    until = time.monotonic() + seconds
    while not flood_stop.is_set() and time.monotonic() < until:
        await asyncio.sleep(0.25)


async def flood_worker(deadline):
    while not flood_stop.is_set() and time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection('catalog-b', 8000), 5)
            try:
                while not flood_stop.is_set() and time.monotonic() < deadline:
                    writer.write(FLOOD_REQUEST)
                    head = (await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)).decode('latin-1').split('\r\n')
                    fields = {k.strip().lower(): v.strip() for k, _, v in (line.partition(':') for line in head[1:] if line)}
                    await reader.readexactly(int(fields.get('content-length', '0')))
                    flood_stats['sent'] += 1
                    if head[0].split()[1] == '429':
                        flood_stats['rejected'] += 1
                        # Waiting a random extra share spreads the retries: 160 connections waking together would
                        # arrive as one burst every Retry-After seconds (measured: a third of cycles turned slow).
                        await pause(min(int(fields.get('retry-after', '1')), 30) * random.uniform(1, 2))
            finally:
                writer.close()
        except (OSError, ValueError, IndexError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            flood_stats['errors'] += 1
            await pause(0.5)


def run_flood(deadline):
    async def main():
        await asyncio.gather(*(flood_worker(deadline) for _ in range(FLOOD_CONNECTIONS)))
    asyncio.run(main())


@app.post('/maintenance/flood')
async def flood(x_aegis_fleet_token: str = Header(default='')):
    """Fault injection: start the report exporter. Only the reports service runs it."""
    authorize(x_aegis_fleet_token)
    if ROLE != 'reports' or (any(t.is_alive() for t in flood_threads) and not flood_stop.is_set()):
        return {'changed': False}
    for worker in flood_threads:  # a job told to stop finishes first, so two never overlap
        worker.join(timeout=5)
    flood_stop.clear()
    flood_threads[:] = [threading.Thread(target=run_flood, args=(time.monotonic() + JOB_MAX_S,), name='report-exporter', daemon=True)]
    flood_threads[0].start()
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'export job started', 'connections': FLOOD_CONNECTIONS, 'generation': BOOT})
    return {'changed': True, 'connections': FLOOD_CONNECTIONS}


@app.post('/maintenance/unflood')
async def unflood(x_aegis_fleet_token: str = Header(default='')):
    """Clean-up for reset: stop the report exporter."""
    authorize(x_aegis_fleet_token)
    running = [t for t in flood_threads if t.is_alive()]
    flood_stop.set()
    for worker in running:
        worker.join(timeout=5)
    return {'changed': bool(running), **flood_stats}


@app.post('/maintenance/restore')
async def restore(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: reapply the deployed settings."""
    authorize(x_aegis_fleet_token)
    drifted = sorted(key for key, value in DEPLOYED.items() if config[key] != value)
    config.update(DEPLOYED)
    if drifted:
        LOGS.append({'at': time.time(), 'ok': True, 'event': 'configuration restored', 'keys': drifted, 'generation': BOOT})
    return {'changed': bool(drifted), 'restored': drifted}
