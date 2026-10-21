"""Real synthetic services. Each instance is an independent Linux process."""
import errno
import hmac
import os
import json
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
TRAFFIC = deque(maxlen=4000)  # (at, client, status) for business requests, newest last
TRAFFIC_WINDOW_S = 60
tcp_last = None  # TCP counters at the previous network check, so retransmissions are reported per interval


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def count_traffic(request, call_next):
    response = await call_next(request)
    if request.url.path in ('/summary', '/catalog', '/ready'):
        TRAFFIC.append((time.time(), request.headers.get('x-aegis-client', 'unidentified')[:40], response.status_code))
    return response


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
    return {'live': True, 'role': ROLE, 'generation': BOOT}


def load_catalogue():
    """Items from the configured source. Only the built-in dataset is deployed, so any file source is missing."""
    source = config.get('catalogue_source', 'builtin:lab-v1')
    if source.startswith('file:'):
        return json.loads(Path(source[5:]).read_text())['items']
    return [{'sku': 'LAB-001', 'name': 'Synthetic component', 'quantity': 12}]


def over_quota(client):
    """True when this caller has already used its quota of accepted requests in the past second."""
    limit = quota.get(client)
    if limit is None:
        return False
    now = time.time()
    return sum(1 for at, who, status in reversed(TRAFFIC) if now - at <= 1 and who == client and status < 400) >= limit


@app.get('/catalog')
async def catalog(x_aegis_client: str = Header(default='unidentified')):
    started = time.monotonic()
    if over_quota(x_aegis_client):
        result = {'ok': False, 'error': 'QuotaExceeded', 'client': x_aegis_client[:40], 'limit_rps': quota[x_aegis_client],
                  'detail': 'per-client request quota exhausted', 'generation': BOOT, 'duration_ms': round((time.monotonic() - started) * 1000, 1)}
        LOGS.append({'at': time.time(), **result})
        return JSONResponse(result, status_code=429)
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


@app.get('/diagnostics/traffic')
async def traffic():
    """Business requests per caller over the last minute, and the quotas this service enforces."""
    now = time.time()
    callers = {}
    for at, client, status in TRAFFIC:
        if now - at > TRAFFIC_WINDOW_S:
            continue
        entry = callers.setdefault(client, {'client': client, 'requests': 0, 'rejected': 0, 'errors': 0})
        entry['requests'] += 1
        entry['rejected'] += status == 429
        entry['errors'] += status >= 500
    for entry in callers.values():
        entry['rps'] = round(entry['requests'] / TRAFFIC_WINDOW_S, 2)
    quotas = [{'client': client, 'limit_rps': quota[client], 'deployed_rps': deployed, 'changed': quota[client] != deployed}
              for client, deployed in QUOTA_DEPLOYED.items()]
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


@app.post('/maintenance/requota')
async def requota(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: reapply the deployed per-caller quotas."""
    global quota_source
    authorize(x_aegis_fleet_token)
    changed = sorted(client for client, limit in QUOTA_DEPLOYED.items() if quota[client] != limit)
    if not changed:
        return {'changed': False}
    quota.update(QUOTA_DEPLOYED)
    quota_source = {'pushed_by': 'deployment', 'at': time.time()}
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'quota policy restored', 'clients': changed, 'generation': BOOT})
    return {'changed': True, 'restored': changed}


@app.post('/maintenance/restore')
async def restore(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: reapply the deployed settings."""
    authorize(x_aegis_fleet_token)
    drifted = sorted(key for key, value in DEPLOYED.items() if config[key] != value)
    config.update(DEPLOYED)
    if drifted:
        LOGS.append({'at': time.time(), 'ok': True, 'event': 'configuration restored', 'keys': drifted, 'generation': BOOT})
    return {'changed': bool(drifted), 'restored': drifted}
