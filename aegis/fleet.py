"""Real synthetic services. Each instance is an independent Linux process."""
import os
import json
import time
import uuid
from contextlib import asynccontextmanager
from collections import deque

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

ROLE = os.getenv('AEGIS_ROLE', 'catalog')
UPSTREAM = os.getenv('AEGIS_UPSTREAM', 'http://catalog-b:8000')
BOOT = str(uuid.uuid4())
LOGS = deque(maxlen=200)


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get('/live')
async def live():
    return {'live': True, 'role': ROLE, 'generation': BOOT}


@app.get('/catalog')
async def catalog():
    return {'items': [{'sku': 'LAB-001', 'name': 'Synthetic component', 'quantity': 12}], 'generation': BOOT}


async def dependency():
    started = time.monotonic()
    correlation = str(uuid.uuid4())
    try:
        response = await app.state.client.get(UPSTREAM + '/catalog', headers={'X-Correlation-ID': correlation})
        response.raise_for_status()
        result = {'ok': True, 'dependency': 'catalog-b', 'correlation': correlation, 'catalog': response.json()}
        status = 200
    except (httpx.HTTPError, ValueError) as error:
        result = {'ok': False, 'dependency': 'catalog-b', 'correlation': correlation, 'error': type(error).__name__}
        status = 503
    result['generation'] = BOOT
    result['duration_ms'] = round((time.monotonic() - started) * 1000, 1)
    LOGS.append({'at': time.time(), **{k: v for k, v in result.items() if k != 'catalog'}})
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
