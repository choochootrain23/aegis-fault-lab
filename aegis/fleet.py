"""Real synthetic services. Each instance is an independent Linux process."""
import errno
import hmac
import os
import json
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


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


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
    stale = sorted(folder.iterdir())[:-RECEIPTS]
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


@app.get('/catalog')
async def catalog():
    started = time.monotonic()
    try:
        write_receipt('catalog')
    except OSError as error:
        return storage_failure(error, started)
    return {'items': [{'sku': 'LAB-001', 'name': 'Synthetic component', 'quantity': 12}], 'generation': BOOT}


async def dependency():
    started = time.monotonic()
    correlation = str(uuid.uuid4())
    try:
        response = await app.state.client.get(UPSTREAM + '/catalog', headers={'X-Correlation-ID': correlation})
        response.raise_for_status()
        result = {'ok': True, 'dependency': 'catalog-b', 'correlation': correlation, 'catalog': response.json()}
        status = 200
    except httpx.HTTPStatusError as error:
        result = {'ok': False, 'dependency': 'catalog-b', 'correlation': correlation, 'error': 'HTTPStatusError',
                  'upstream_status': error.response.status_code}
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


@app.get('/ready')
async def ready():
    if ROLE == 'gateway':
        return await dependency()
    return {'ok': True, 'generation': BOOT}


@app.get('/summary')
async def summary():
    if ROLE == 'gateway':
        return await dependency()
    return await catalog()


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
