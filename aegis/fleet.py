"""Real synthetic services. Each instance is an independent Linux process."""
import asyncio
import contextlib
import errno
import hmac
import os
import json
import random
import re
import socket
import struct
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

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
QUOTA_DEPLOYED = {'gateway-a': 300, 'gateway-c': 300} if ROLE == 'catalog' else {}  # room for real user traffic through each gateway
quota = dict(QUOTA_DEPLOYED)
quota_source = {'pushed_by': 'deployment', 'at': time.time()}
TRAFFIC_WINDOW_S = 60
# Business requests per caller in one-second buckets, newest last: [second, {client: [requests, rejected, errors, accepted, retries]}].
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
# Name resolution. A gateway resolves the catalogue's name itself, as a stub resolver does: a real DNS query over UDP
# to the nameserver in its resolv.conf (Docker's embedded DNS), each answer cached for at most DNS_TTL_S. The DNS
# fault repoints it at 127.0.0.53, systemd-resolved's stub address, a setting easily copied from a host image;
# nothing listens there inside a container, so every lookup is refused once the cached answer expires.
DNS_PORT = 53
DNS_TTL_S = 5
DNS_NEGATIVE_S = 1    # a failed lookup is retried after this, so a broken resolver is not asked on every request
BROKEN_NAMESERVER = '127.0.0.53'


def system_nameserver():
    try:
        return next((line.split()[1] for line in Path('/etc/resolv.conf').read_text().splitlines()
                     if len(line.split()) >= 2 and line.split()[0] == 'nameserver'), None)
    except OSError:
        return None


SYSTEM_NAMESERVER = system_nameserver()
resolver = {'nameserver': SYSTEM_NAMESERVER, 'pushed_by': 'deployment', 'at': time.time()}
dns_cache = {}   # name -> {'address', 'expires', 'error', 'last_good'}
dns_lookups = {}  # name -> the lookup in flight, shared by every request waiting on it


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
    async with httpx.AsyncClient(timeout=1, trust_env=False, limits=httpx.Limits(max_connections=100, max_keepalive_connections=100)) as client:
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
        headers = dict(scope['headers'])
        client = headers.get(b'x-aegis-client', b'unidentified').decode('latin-1')[:40]
        retry = headers.get(b'x-aegis-attempt', b'1') not in (b'1', b'')

        async def counted(message):
            if message['type'] == 'http.response.start':
                tally(client, message['status'], retry)
            await send(message)
        await self.app(scope, receive, counted)


class DeadlineGuard:
    """Drops work whose caller has already given up. Callers send X-Aegis-Deadline (seconds since the epoch, on the
    shared host clock); a request reached after it is answered 504 at once, without doing the work or calling
    upstream. Without this, an overloaded service kept processing thousands of abandoned requests for minutes
    after the load stopped. Requests without the header are unaffected."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and (deadline := request_deadline(dict(scope['headers']).get(DEADLINE_HEADER))) and time.time() > deadline:
            EXPIRED['count'] += 1
            await send({'type': 'http.response.start', 'status': 504, 'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': b'{"ok": false, "error": "DeadlineExceeded", "detail": "the caller had already given up"}'})
            return
        await self.app(scope, receive, send)


def request_deadline(raw):
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


class LoadShedder:
    """Counts business requests in flight and, when enabled, caps them, turning the rest away at once with 503 so the
    work the service does accept finishes quickly. Off by default; the remedy for a retry storm turns it on."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] not in ('/summary', '/catalog', '/ready'):
            return await self.app(scope, receive, send)
        if shedding['in_flight'] > SHED_LIMIT:
            shedding['overloaded_at'] = time.time()  # more queued work than a shedding service would keep
        if shedding['limit'] is not None and shedding['in_flight'] >= shedding['limit']:
            shedding['shed'] += 1
            await send({'type': 'http.response.start', 'status': 503, 'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': b'{"ok": false, "error": "Overloaded", "detail": "load shed: too many requests in flight"}'})
            return
        shedding['in_flight'] += 1
        try:
            await self.app(scope, receive, send)
        finally:
            shedding['in_flight'] -= 1


SHED_LIMIT = 12  # business requests a shedding catalogue keeps in flight: enough to stay busy, few enough to stay fast
shedding = {'limit': None, 'shed': 0, 'in_flight': 0, 'overloaded_at': 0.0}
DEADLINE_HEADER = b'x-aegis-deadline'
EXPIRED = {'count': 0}  # requests dropped because their caller had already given up
app.add_middleware(LoadShedder)
app.add_middleware(DeadlineGuard)
app.add_middleware(CountTraffic)


def tally(client, status, retry=False):
    second = int(time.time())
    if not TRAFFIC or TRAFFIC[-1][0] != second:
        TRAFFIC.append([second, {}])
    counts = TRAFFIC[-1][1].setdefault(client, [0, 0, 0, 0, 0])
    counts[0] += 1
    counts[1] += status == 429
    counts[2] += status >= 500
    counts[3] += status < 400
    counts[4] += retry


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
    return bool(TRAFFIC) and TRAFFIC[-1][0] == int(time.time()) and TRAFFIC[-1][1].get(client, [0, 0, 0, 0, 0])[3] >= limit


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


class NameResolutionError(Exception):
    """A lookup that produced no address; the message says why."""


def dns_query(name, nameserver, timeout=1.0):
    """One real DNS query for name's A record over UDP; returns (address, ttl)."""
    ident = random.randrange(65536)
    question = b''.join(bytes([len(label)]) + label.encode() for label in name.split('.')) + b'\0\0\x01\0\x01'
    packet = struct.pack('>HHHHHH', ident, 0x0100, 1, 0, 0, 0) + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((nameserver, DNS_PORT))
        sock.send(packet)
        reply = sock.recv(512)
    if len(reply) < 12 or reply[:2] != packet[:2]:
        raise NameResolutionError(f'{name}: malformed reply from nameserver {nameserver}')
    if (code := reply[3] & 0x0F):
        raise NameResolutionError(f"{name}: {({2: 'SERVFAIL', 3: 'NXDOMAIN', 5: 'REFUSED'}).get(code, f'rcode {code}')} from nameserver {nameserver}")
    offset = 12 + len(question)
    for _ in range(struct.unpack('>H', reply[6:8])[0]):
        if reply[offset] & 0xC0 == 0xC0:  # a compressed name is a two-byte pointer
            offset += 2
        else:
            while reply[offset]:
                offset += reply[offset] + 1
            offset += 1
        kind, _, ttl, length = struct.unpack('>HHIH', reply[offset:offset + 10])
        offset += 10
        if kind == 1 and length == 4:
            return socket.inet_ntoa(reply[offset:offset + 4]), ttl
        offset += length
    raise NameResolutionError(f'{name}: no address in the reply from nameserver {nameserver}')


def lookup_failure(name, nameserver, error):
    if isinstance(error, NameResolutionError):
        return str(error)
    if isinstance(error, ConnectionRefusedError):
        return f'{name}: connection refused by nameserver {nameserver}'
    if isinstance(error, TimeoutError):
        return f'{name}: no answer from nameserver {nameserver}'
    return f'{name}: {type(error).__name__} from nameserver {nameserver}'


def literal_address(host):
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return host == 'localhost'


async def resolve(host):
    """The address for host: from the cache while its answer is fresh, otherwise from one shared real lookup."""
    if literal_address(host) or not resolver['nameserver']:
        return host
    entry = dns_cache.get(host) or {}
    if entry.get('expires', 0) > time.monotonic():
        if entry.get('error'):
            raise NameResolutionError(entry['error'])
        return entry['address']
    nameserver = resolver['nameserver']
    task = dns_lookups.get(host)
    if task is None or task.done():
        task = dns_lookups[host] = asyncio.ensure_future(asyncio.to_thread(dns_query, host, nameserver))
    try:
        address, ttl = await asyncio.shield(task)  # one caller's deadline must not cancel everyone's lookup
    except (OSError, NameResolutionError) as error:
        detail = lookup_failure(host, nameserver, error)
        dns_cache[host] = {**entry, 'address': None, 'error': detail, 'expires': time.monotonic() + DNS_NEGATIVE_S}
        raise NameResolutionError(detail) from error
    dns_cache[host] = {'address': address, 'error': None, 'last_good': address, 'expires': time.monotonic() + min(ttl, DNS_TTL_S)}
    return address


async def upstream_request(path, headers, timeout):
    """GET path from the configured upstream, resolving its name first, as every gateway call to the catalogue does."""
    target = urlsplit(config['upstream'])
    address = await resolve(target.hostname)
    url = f'{target.scheme}://{address}' + (f':{target.port}' if target.port else '') + path
    return await app.state.client.get(url, headers={**headers, 'Host': target.netloc}, timeout=timeout)


# A bulkhead for calls to the catalogue: fewer slots than the HTTP client's pool, so requests wait here, where a
# deadline can safely end the wait, and never inside the client's pool, where a timed-out wait leaked connections
# for good under a retry storm (measured: every gateway stuck on PoolTimeout after the load stopped).
UPSTREAM_SLOTS = asyncio.Semaphore(80)


async def call_catalogue(correlation, upstream_deadline):
    try:
        response = await upstream_request('/catalog', {'X-Correlation-ID': correlation, 'X-Aegis-Client': NAME, 'X-Aegis-Deadline': f'{upstream_deadline:.3f}'},
                                          config['upstream_timeout_s'])
        response.raise_for_status()
        return {'ok': True, 'dependency': 'catalog-b', 'correlation': correlation, 'catalog': response.json()}, 200
    except NameResolutionError as error:
        return {'ok': False, 'dependency': 'catalog-b', 'correlation': correlation, 'error': 'NameResolutionError', 'detail': str(error)}, 503
    except httpx.HTTPStatusError as error:
        return {'ok': False, 'dependency': 'catalog-b', 'correlation': correlation, 'error': 'HTTPStatusError',
                'upstream_status': error.response.status_code, 'upstream_error': upstream_error(error.response)}, 503
    except (httpx.HTTPError, ValueError) as error:
        return {'ok': False, 'dependency': 'catalog-b', 'correlation': correlation, 'error': type(error).__name__}, 503


async def dependency(deadline=None):
    started = time.monotonic()
    if (failure := release_failure(started)):
        return failure
    correlation = str(uuid.uuid4())
    # Pass on the tighter of our caller's deadline and our own timeout, so the catalogue can drop what we abandon.
    upstream_deadline = min(deadline or float('inf'), time.time() + config['upstream_timeout_s'])
    try:
        await asyncio.wait_for(UPSTREAM_SLOTS.acquire(), max(0.0, upstream_deadline - time.time()))
    except TimeoutError:
        result, status = {'ok': False, 'dependency': 'catalog-b', 'correlation': correlation, 'error': 'UpstreamBusy'}, 503
    else:
        try:
            result, status = await call_catalogue(correlation, upstream_deadline)
        finally:
            UPSTREAM_SLOTS.release()
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
async def ready(x_aegis_deadline: str = Header(default='')):
    if ROLE == 'gateway':
        return await dependency(request_deadline(x_aegis_deadline))
    return {'ok': True, 'generation': BOOT}


@app.get('/summary')
async def summary(x_aegis_client: str = Header(default='unidentified'), x_aegis_deadline: str = Header(default='')):
    if ROLE == 'gateway':
        return await dependency(request_deadline(x_aegis_deadline))
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


async def dns_report():
    """A fresh lookup of the upstream's name through the configured nameserver, bypassing the cache. When it fails,
    a TCP connection to the last address that resolved shows whether the service itself is still reachable."""
    target = urlsplit(config['upstream'])
    if literal_address(target.hostname) or not resolver['nameserver']:
        return None
    nameserver, began = resolver['nameserver'], time.monotonic()
    report = {'name': target.hostname, 'nameserver': nameserver, 'deployed_nameserver': SYSTEM_NAMESERVER, 'changed': nameserver != SYSTEM_NAMESERVER,
              'pushed_by': resolver['pushed_by']}
    try:
        address, ttl = await asyncio.to_thread(dns_query, target.hostname, nameserver)
        report.update(ok=True, address=address, ttl_s=ttl)
    except (OSError, NameResolutionError) as error:
        report.update(ok=False, error=lookup_failure(target.hostname, nameserver, error))
    report['lookup_ms'] = round((time.monotonic() - began) * 1000, 1)
    last_good = (dns_cache.get(target.hostname) or {}).get('last_good')
    if not report['ok'] and last_good:
        began = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(last_good, target.port or 80), 1)
            writer.close()
            report.update(last_good_address=last_good, connect_last_good_ms=round((time.monotonic() - began) * 1000, 1))
        except (OSError, TimeoutError) as error:
            report.update(last_good_address=last_good, connect_last_good_error=type(error).__name__)
    return report


@app.get('/diagnostics/network')
async def network():
    return {**read_qdiscs(), 'tcp': retransmissions(), **({'dns': await dns_report()} if ROLE == 'gateway' else {}), 'generation': BOOT}


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
        for client, (requests, rejected, errors, _, retries) in counts.items():
            entry = callers.setdefault(client, {'client': client, 'requests': 0, 'rejected': 0, 'errors': 0, 'retries': 0})
            entry['requests'] += requests
            entry['rejected'] += rejected
            entry['errors'] += errors
            entry['retries'] += retries
    recent, recent_retries = {}, {}
    for second, counts in traffic_since(time.time() - RECENT_S):
        for client, (requests, _, _, _, retries) in counts.items():
            recent[client] = recent.get(client, 0) + requests
            recent_retries[client] = recent_retries.get(client, 0) + retries
    for entry in callers.values():
        entry['rps'] = round(entry['requests'] / TRAFFIC_WINDOW_S, 2)
        # What it is sending now, not a minute ago: a storm or a flood is judged by its current rate.
        entry['recent_rps'] = round(recent.get(entry['client'], 0) / RECENT_S, 2)
        entry['recent_retries'] = recent_retries.get(entry['client'], 0)
    quotas = [{'client': client, 'limit_rps': limit, 'deployed_rps': QUOTA_DEPLOYED.get(client), 'changed': limit != QUOTA_DEPLOYED.get(client)}
              for client, limit in quota.items()]
    return {'window_s': TRAFFIC_WINDOW_S, 'clients': sorted(callers.values(), key=lambda e: -e['requests']), 'quotas': quotas,
            'shedding': {'enabled': shedding['limit'] is not None, 'max_in_flight': shedding['limit'], 'shed': shedding['shed'], 'in_flight': shedding['in_flight']},
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
        host = urlsplit(config['upstream']).hostname
        try:
            await resolve(host)
            ok = span('dns lookup ' + host, began, True)
        except NameResolutionError as error:
            ok = span('dns lookup ' + host, began, False, error='NameResolutionError', detail=str(error))
        if ok:
            name, began = 'upstream ' + config['upstream'].split('//')[-1] + '/catalog', time.monotonic()
            try:
                response = await upstream_request('/catalog', {'X-Aegis-Client': NAME}, config['upstream_timeout_s'])
                ok = span(name, began, response.is_success, status=response.status_code)
            except (httpx.HTTPError, NameResolutionError) as error:
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


@app.post('/maintenance/repoint')
async def repoint(x_aegis_fleet_token: str = Header(default='')):
    """Fault injection: a node-settings sync that points this gateway's resolver at a nameserver that is not there."""
    authorize(x_aegis_fleet_token)
    if ROLE != 'gateway' or not SYSTEM_NAMESERVER or literal_address(urlsplit(config['upstream']).hostname):
        raise HTTPException(409, 'Only a gateway that resolves its upstream by name has a resolver to repoint')
    if resolver['nameserver'] == BROKEN_NAMESERVER:
        return {'changed': False}
    resolver.update(nameserver=BROKEN_NAMESERVER, pushed_by='node-settings-sync', at=time.time())
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'resolver settings updated', 'nameserver': BROKEN_NAMESERVER, 'pushed_by': 'node-settings-sync', 'generation': BOOT})
    return {'changed': True, 'nameserver': BROKEN_NAMESERVER}


@app.post('/maintenance/resolver')
async def restore_resolver(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: point the resolver back at the nameserver this service booted with, and forget failed lookups."""
    authorize(x_aegis_fleet_token)
    if resolver['nameserver'] == SYSTEM_NAMESERVER:
        return {'changed': False}
    resolver.update(nameserver=SYSTEM_NAMESERVER, pushed_by='operator', at=time.time())
    for name in [name for name, entry in dns_cache.items() if entry.get('error')]:
        dns_cache.pop(name)
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'resolver settings restored', 'nameserver': SYSTEM_NAMESERVER, 'generation': BOOT})
    return {'changed': True, 'nameserver': SYSTEM_NAMESERVER}


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
# itself after JOB_MAX_S, and honours Retry-After like a well-behaved client, so rate-limiting it works. 256
# connections queue each request about half a second behind the flood (measured; at 160, cheaper request handling had
# brought the wait just under the 300 ms slow threshold).
FLOOD_CONNECTIONS = 256
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


@app.post('/maintenance/hiccup')
async def hiccup(x_aegis_fleet_token: str = Header(default='')):
    """A short index compaction that uses the whole CPU for HICCUP_S and then stops by itself: the trigger of a retry storm."""
    authorize(x_aegis_fleet_token)
    if any(t.is_alive() for t in job_threads):
        return {'changed': False}
    job_stop.clear()
    job_threads.clear()
    deadline = time.monotonic() + HICCUP_S
    for index in range(JOB_THREADS):
        worker = threading.Thread(target=busy, args=(deadline,), name=f'compaction-{index}', daemon=True)
        worker.start()
        job_threads.append(worker)
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'background job started', 'job': 'index compaction', 'expected_s': HICCUP_S, 'generation': BOOT})
    return {'changed': True, 'seconds': HICCUP_S}


@app.post('/maintenance/shed')
async def shed(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: turn on load shedding at a gateway, where user traffic arrives. It only takes effect on a gateway that
    has had more work queued than it would keep in the last 30 seconds; anywhere else nothing changes."""
    authorize(x_aegis_fleet_token)
    if ROLE != 'gateway' or shedding['limit'] is not None or time.time() - shedding['overloaded_at'] > 30:
        return {'changed': False}
    shedding.update(limit=SHED_LIMIT, shed=0)
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'load shedding enabled', 'max_in_flight': SHED_LIMIT, 'generation': BOOT})
    return {'changed': True, 'max_in_flight': SHED_LIMIT}


@app.post('/maintenance/unshed')
async def unshed(x_aegis_fleet_token: str = Header(default='')):
    """Clean-up for reset: turn load shedding off again."""
    authorize(x_aegis_fleet_token)
    if shedding['limit'] is None:
        return {'changed': False}
    shedding.update(limit=None)
    return {'changed': True, 'shed': shedding['shed']}


# App users, run by the reports service for the retry-storm fault: open-loop arrivals (they keep coming however slow the
# service gets) through both gateways, each retrying a failure at once up to USER_RETRIES times with no backoff, the
# client flaw that turns a short slowdown into an overload. Every attempt carries a deadline and its attempt number.
USER_RPS = 60          # new users a second, all through the target gateway: about 40% of what it can serve
USER_RETRIES = 3
USER_DEADLINE_S = 2
HICCUP_S = 8           # how long the catalogue's compaction job runs
DAYTIME_RPS = 32       # steady users a second through each gateway while a power cap is active: clearly over a capped gateway
HICCUP_AFTER_S = 6     # how long the users run normally before it
users_stop = threading.Event()
users_threads = []
users_stats = {'users': 0, 'succeeded': 0, 'gave_up': 0, 'attempts': 0}


async def user_attempt(host, attempt):
    users_stats['attempts'] += 1
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, 8000), USER_DEADLINE_S)
        try:
            writer.write((f'GET /summary HTTP/1.1\r\nHost: {host}\r\nX-Aegis-Client: mobile-app\r\nX-Aegis-Attempt: {attempt}\r\n'
                          f'X-Aegis-Deadline: {time.time() + USER_DEADLINE_S:.3f}\r\nConnection: close\r\n\r\n').encode())
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), USER_DEADLINE_S)
            return head.split(b' ')[1] == b'200'
        finally:
            writer.close()
    except (OSError, ValueError, IndexError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return False


STORM_TARGETS = ('gateway-a', 'gateway-c')


async def app_user(host, retries):
    users_stats['users'] += 1
    for attempt in range(1, retries + 2):
        if await user_attempt(host, attempt):
            users_stats['succeeded'] += 1
            return
    users_stats['gave_up'] += 1


def run_users(deadline, hosts, rate, retries):
    async def main():
        tasks, began, sent = set(), time.monotonic(), 0
        while not users_stop.is_set() and time.monotonic() < deadline:
            for host in hosts:
                if len(tasks) < 2000:  # a hard ceiling, whatever happens downstream
                    task = asyncio.create_task(app_user(host, retries))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
            sent += 1
            await asyncio.sleep(max(0.0, began + sent / rate - time.monotonic()))
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(main())


@app.post('/maintenance/storm')
async def storm(x_aegis_fleet_token: str = Header(default=''), x_aegis_target: str = Header(default='')):
    """Fault injection: start the app users on one gateway, then ask the catalogue for its short compaction job.
    Reports service only; the target must be one of the two gateways."""
    authorize(x_aegis_fleet_token)
    if x_aegis_target not in STORM_TARGETS:
        raise HTTPException(422, 'The app users only call a gateway')
    if ROLE != 'reports' or (any(t.is_alive() for t in users_threads) and not users_stop.is_set()):
        return {'changed': False}
    for worker in users_threads:
        worker.join(timeout=5)
    users_stop.clear()
    users_threads[:] = [threading.Thread(target=run_users, args=(time.monotonic() + JOB_MAX_S, (x_aegis_target,), USER_RPS, USER_RETRIES), name='app-users', daemon=True)]
    users_threads[0].start()
    asyncio.get_running_loop().call_later(HICCUP_AFTER_S, lambda: asyncio.ensure_future(trigger_hiccup()))
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'app users started', 'gateway': x_aegis_target, 'rps': USER_RPS, 'retries': USER_RETRIES, 'generation': BOOT})
    return {'changed': True}


async def trigger_hiccup():
    with contextlib.suppress(httpx.HTTPError):
        await app.state.client.post('http://catalog-b:8000/maintenance/hiccup', headers={'X-Aegis-Fleet-Token': FLEET_TOKEN}, timeout=5)


@app.post('/maintenance/daytime')
async def daytime(x_aegis_fleet_token: str = Header(default='')):
    """Fault injection support: steady daytime users through both gateways, with no retry flaw. Reports service only."""
    authorize(x_aegis_fleet_token)
    if ROLE != 'reports' or (any(t.is_alive() for t in users_threads) and not users_stop.is_set()):
        return {'changed': False}
    for worker in users_threads:
        worker.join(timeout=5)
    users_stop.clear()
    users_threads[:] = [threading.Thread(target=run_users, args=(time.monotonic() + JOB_MAX_S, STORM_TARGETS, DAYTIME_RPS, 0), name='daytime-users', daemon=True)]
    users_threads[0].start()
    LOGS.append({'at': time.time(), 'ok': True, 'event': 'daytime users started', 'rps_per_gateway': DAYTIME_RPS, 'generation': BOOT})
    return {'changed': True}


@app.post('/maintenance/unstorm')
async def unstorm(x_aegis_fleet_token: str = Header(default='')):
    """Clean-up for reset: stop the app users."""
    authorize(x_aegis_fleet_token)
    running = [t for t in users_threads if t.is_alive()]
    users_stop.set()
    for worker in running:
        worker.join(timeout=10)
    return {'changed': bool(running), **users_stats}


@app.post('/maintenance/restore')
async def restore(x_aegis_fleet_token: str = Header(default='')):
    """Recovery: reapply the deployed settings."""
    authorize(x_aegis_fleet_token)
    drifted = sorted(key for key, value in DEPLOYED.items() if config[key] != value)
    config.update(DEPLOYED)
    if drifted:
        LOGS.append({'at': time.time(), 'ok': True, 'event': 'configuration restored', 'keys': drifted, 'generation': BOOT})
    return {'changed': bool(drifted), 'restored': drifted}
