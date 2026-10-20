"""Local operator API and persisted incident workflow."""
import asyncio
import contextlib
import hmac
import json
import os
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from aegis.store import Store
from aegis.guidance import explain, SLOW_MS
from aegis.ownership import controller_ownership

NODES = {'gateway-a': 'http://gateway-a:8000', 'catalog-b': 'http://catalog-b:8000'}
CHECKS = {'liveness': '/live', 'readiness': '/ready', 'business': '/summary', 'logs': '/diagnostics/logs', 'disk': '/diagnostics/disk',
          'config': '/diagnostics/config', 'trace': '/diagnostics/trace'}
BROKER_CHECKS = {'service': 'inspect', 'resources': 'resources'}
TERMINAL = {'ended'}
# Each fault kind: the injection verb and the remedy that reverses it.
FAULTS = {'service-unavailable': ('stop', 'start'), 'cpu-starvation': ('burn', 'calm'), 'disk-full': ('fill', 'clear'),
          'config-drift': ('drift', 'restore')}
REMEDIES = {remedy for _, remedy in FAULTS.values()}
NODE_VERBS = {'fill', 'clear', 'burn', 'calm', 'drift', 'restore'}  # applied through the service's token-protected maintenance endpoint
UNCHANGED = {'start': 'Already running; nothing changed', 'stop': 'Already stopped; nothing changed',
             'calm': 'No background job running; nothing changed', 'burn': 'A background job was already running; nothing changed',
             'clear': 'No filler file present; nothing changed', 'fill': 'Nothing changed',
             'restore': 'Running config already matches the deployed settings; nothing changed', 'drift': 'Setting already overridden; nothing changed'}
DEBRIEFS = {
    'service-unavailable': 'The scenario requested stopping {target}. When that succeeds, its container state reads stopped and its own probes fail. When the catalogue stops, the gateway stays live but its business requests fail, so the failure spreads; when the gateway stops, the catalogue stays healthy, so it does not. Starting the stopped service restores the request path.',
    'cpu-starvation': 'The scenario requested a runaway background job inside {target}. When that succeeds, the service keeps running and answering, but slowly: the job uses the container\'s whole 0.25-CPU limit, the kernel throttles it, and requests queue behind it, crossing the {slow} ms threshold. Its log records the job starting. A slow catalogue makes the gateway slow, and sometimes time out, too. Stopping the job returns normal latency.',
    'disk-full': 'The scenario requested filling {target}\'s 8 MiB /tmp with one large file. When that succeeds, the process stays live, but every business request must write a small receipt, so writes fail with ENOSPC (HTTP 507). A full catalogue fails the gateway\'s requests too; a full gateway fails only its own. Disk usage shows 100% and the filler as the largest file. Clearing it restores writes.',
    'config-drift': 'The scenario requested a bad runtime configuration reload on {target}. When that succeeds, the process stays live and its container keeps running, so liveness, container state, CPU and disk all look normal. On the gateway, the upstream address points at a port nothing listens on, so every business request fails to connect, which looks like a stopped catalogue until the catalogue is checked directly. On the catalogue, the data source points at a dataset that was never deployed, so its business requests fail with HTTP 500 and the gateway\'s fail with them. The running config shows the drifted setting next to its deployed value, and the log records the reload. Restoring the deployed config fixes it.',
}


def debrief(s):
    text = DEBRIEFS[s.get('fault_kind', 'service-unavailable')].format(target=s['fault_target'], slow=SLOW_MS)
    return (text + ' Injection outcome: ' + s.get('fault_action', {}).get('status', 'not recorded') + '. The action record explains the exercise but was not evidence available during the investigation. Other evidence-supported investigation orders are valid.')


FaultKind = Literal['service-unavailable', 'cpu-starvation', 'disk-full', 'config-drift']
NodeName = Literal['gateway-a', 'catalog-b']
CheckKind = Literal['liveness', 'readiness', 'business', 'logs', 'service', 'resources', 'disk', 'config', 'trace']


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Start(Input):
    mode: Literal['practice', 'challenge']
    target: NodeName = 'catalog-b'
    kind: FaultKind = 'service-unavailable'
    key: str = Field(min_length=8, max_length=80)


class Check(Input):
    node: NodeName
    kind: CheckKind


class SessionInput(Input):
    session_id: str = Field(min_length=1, max_length=80)


class Action(SessionInput):
    node: NodeName = 'catalog-b'
    key: str = Field(min_length=8, max_length=80)


class Recover(Action):
    verb: Literal['start', 'calm', 'clear', 'restore'] = 'start'


class Note(SessionInput):
    explanation: str = Field(max_length=3000)
    uncertainty: str = Field(default='', max_length=1500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class Help(SessionInput):
    level: Literal['evidence', 'hint', 'action']


class Lab:
    def __init__(self, store, client, nodes=None, resource_check=True):
        self.store, self.client = store, client
        self.nodes = nodes or NODES
        self.token = os.getenv('AEGIS_BROKER_TOKEN', '')
        self.fleet_token = os.getenv('AEGIS_FLEET_TOKEN', '')
        self.broker_url = os.getenv('AEGIS_BROKER_URL', 'http://broker:8000')
        self.resource_check = resource_check
        self.lock = asyncio.Lock()
        self.latest = {}
        self.baseline = 0
        self.successes = 0
        self.healthy_observations = []
        self.failures = 0
        self.collector_error = None
        self.last_cycle = None
        self.session = store.active_session()
        store.reconcile()
        if self.session:
            existing = {a['key'] for a in self.session['recovery_attempts']}
            reset_prefix = self.session.get('reset_key', '')
            for action in store.actions(self.session['id']):
                body = action['body']
                if body.get('verb') in REMEDIES and body.get('status') in ('unknown', 'succeeded') and action['key'] not in existing and not (reset_prefix and action['key'].startswith(reset_prefix + '-')):
                    self.session['recovery_attempts'].append({'key': action['key'], 'at': action['at'], **body})
                    if self.session['status'] in ('observing', 'investigating'):
                        self.session['status'] = 'verifying'
            injection = store.action(self.session.get('start_key', ''))
            if injection and 'fault_action' not in self.session:
                self.session['fault_action'] = injection['body']
            self.session['verification'] = 'Awaiting fresh checks after controller restart'
            if self.session['status'] == 'resetting' and len(self.session.get('reset', {}).get('results', [])) < len(self.reset_plan(self.session)):
                self.session['verification'] = 'Reset was interrupted. Inspect current state or retry reset; no action was replayed.'
            store.save(self.session)

    def admission(self):
        if not self.resource_check:
            return
        memory = next(int(line.split()[1]) * 1024 for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))
        if memory < 1024 ** 3 or shutil.disk_usage(Path(os.getenv('AEGIS_STATE', 'state/lab.sqlite')).parent).free < 10 * 1024 ** 3:
            raise HTTPException(409, 'Insufficient memory or disk headroom for a fault/extension')
        db_size = self.store.storage()['live_bytes']
        if db_size > 256 * 1024 ** 2:
            raise HTTPException(409, 'Evidence storage budget reached; export and perform retention before continuing')

    async def broker(self, node, verb, generation=None):
        try:
            response = await self.client.post(self.broker_url + '/operation', headers={'Authorization': 'Bearer ' + self.token},
                                              json={'target': node, 'verb': verb, 'generation': generation}, timeout=10)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, 'Docker broker unavailable or rejected the operation. No success assumed.') from error

    async def check(self, node, kind, origin='operator'):
        if origin == 'operator':
            self.persistence_budget()
        started = time.monotonic()
        try:
            if kind in BROKER_CHECKS:
                body = {'ok': True, **await self.broker(node, BROKER_CHECKS[kind])}
                body.pop('changed', None)
            else:
                async with self.client.stream('GET', self.nodes[node] + CHECKS[kind], timeout=3) as response:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 32768:
                            raise ValueError('Response exceeds evidence limit')
                    body = {'ok': response.is_success, 'http_status': response.status_code, 'result': json.loads(raw)}
            body['duration_ms'] = round((time.monotonic() - started) * 1000, 1)
            if body['ok'] and kind in ('liveness', 'readiness', 'business') and body['duration_ms'] > SLOW_MS:
                body['slow'] = True
            if kind == 'trace' and isinstance(body.get('result'), dict) and body['result'].get('handler_ms') is not None:
                # Time the request spent outside the handler: connection, queueing and scheduling under CPU pressure.
                body['outside_handler_ms'] = round(max(0.0, body['duration_ms'] - body['result']['handler_ms']), 1)
        except (httpx.HTTPError, ValueError, HTTPException) as error:
            body = {'ok': False, 'error': type(error).__name__, 'detail': 'Check unavailable or failed',
                    'duration_ms': round((time.monotonic() - started) * 1000, 1)}
        body['origin'] = origin
        if kind in BROKER_CHECKS and body.get('generation'):
            body['generation_scope'] = 'container'
        elif isinstance(body.get('result'), dict) and body['result'].get('generation'):
            body['process_generation'] = body['result']['generation']
            body['generation_scope'] = 'process'
        sid = self.session['id'] if self.session and self.session['status'] != 'ended' else None
        evidence = self.store.record(sid, node, kind, body)
        self.latest[(node, kind)] = evidence
        return evidence

    def persistence_budget(self):
        usage = self.store.storage()
        if usage['live_bytes'] >= usage['live_limit_bytes'] or usage['wal_bytes'] >= usage['wal_limit_bytes']:
            raise HTTPException(507, 'Evidence storage limit reached. Export evidence and run retention; no action was attempted.')

    async def cycle(self):
        # A single collector loop and API lock prevent overlapping observation/action cycles.
        async with self.lock:
            self.persistence_budget()
            results = await asyncio.gather(*(self.check(n, k, origin='collector') for n in self.nodes for k in ('liveness', 'business')))
            good = all(e['body']['ok'] and not e['body'].get('slow') for e in results)
            self.baseline = self.baseline + 1 if good else 0
            self.successes = self.successes + 1 if good else 0
            if self.successes <= 1:
                self.healthy_observations = []
            if good:
                self.healthy_observations = (self.healthy_observations + [[e['id'] for e in results]])[-3:]
            self.failures = self.failures + 1 if not good else 0
            self.last_cycle = time.time()
            self.collector_error = None
            s = self.session
            if not s or s['status'] == 'ended':
                return
            if self.failures >= 3 and s['status'] == 'observing':
                s['status'] = 'investigating'
                s['detected_at'] = time.time()
                s['verification'] = 'Repeated failures observed; investigate the evidence'
            if s['status'] in ('verifying', 'resetting') and self.successes >= 3:
                if s['status'] == 'resetting':
                    s['status'] = 'ended'
                    s['ended_at'] = time.time()
                    s['outcome'] = s.get('outcome') or 'reset'
                    s['closure'] = 'automatic_reset' if s.get('reset', {}).get('automatic') else 'operator_reset'
                    s['verification'] = 'Baseline verified; this is not a targeted recovery'
                    s['reset_evidence_ids'] = [eid for cycle in self.healthy_observations for eid in cycle]
                else:
                    s['status'] = 'recovered'
                    s['outcome'] = 'verified_recovery'
                    s['recovery_verified_at'] = time.time()
                    s['verification'] = 'Three consecutive healthy fleet and business checks'
                    s['recovery_evidence_ids'] = [eid for cycle in self.healthy_observations for eid in cycle]
                s['verified_at'] = time.time()
            elif s['status'] == 'verifying' and not good:
                s['verification'] = 'Recovery has not restored all affected checks'
            if s['status'] not in ('ended', 'recovered', 'resetting') and time.time() >= s['deadline'] + 60:
                await self.reset_locked('expiry-' + s['id'], automatic=True)
            self.store.save(s)

    def public_session(self, s=None):
        s = s or self.session
        if not s:
            return None
        result = {k: v for k, v in s.items() if k not in ('fault_target', 'fault_kind', 'fault_action', 'start_key', 'reset_key', 'extensions')}
        if s['mode'] == 'practice' or s.get('revealed') or s['status'] == 'ended':
            result['scenario'] = {'kind': s['fault_kind'], 'target': s['fault_target']}
            if s.get('revealed') or s['status'] == 'ended':
                result['debrief'] = debrief(s)
        return result

    def reset_plan(self, session):
        """Reset starts every node, then reverses a non-stop fault on its target."""
        plan = [(node, 'start') for node in self.nodes]
        remedy = FAULTS[session.get('fault_kind', 'service-unavailable')][1]
        if remedy != 'start':
            plan.append((session['fault_target'], remedy))
        return plan

    def state(self):
        now = time.time()
        nodes = []
        for node in self.nodes:
            live = self.latest.get((node, 'liveness'))
            business = self.latest.get((node, 'business'))
            fresh = live and business and all(0 <= now - e['at'] < 6 for e in (live, business))
            fast = fresh and not live['body'].get('slow') and not business['body'].get('slow')
            health = 'unknown' if not fresh else ('unavailable' if not live['body']['ok'] else
                                                  'healthy' if business['body']['ok'] and fast else 'degraded')
            nodes.append({'id': node, 'role': 'gateway' if node == 'gateway-a' else 'catalogue', 'health': health,
                          'live': live, 'business': business})
        return {'nodes': nodes, 'session': self.public_session(), 'baseline_cycles': self.baseline, 'healthy_streak': self.successes,
                'collector_error': self.collector_error, 'last_cycle': self.last_cycle,
                'evidence': self.store.evidence(self.session['id'], 60) if self.session else self.store.baseline_evidence(),
                'ai': {'available': False, 'reason': 'AI integration is a later milestone; manual investigation is fully available.'},
                'now': now, 'history': [self.history_summary(s) for s in self.store.sessions()[:20]],
                'storage': self.store.storage(),
                'retention': {'raw_hours': 24, 'summary_days': 30, 'warning': 'Export supporting evidence before 24 hours. Older references may become unavailable.'}}

    def history_summary(self, session):
        public = self.public_session(session)
        keys = ('id', 'mode', 'status', 'started_at', 'outcome', 'closure', 'revealed', 'recovery_verified_at')
        return {**{k: public.get(k) for k in keys}, 'notes_count': len(public.get('notes', [])),
                'help_count': len(public.get('help', [])), 'evidence': self.store.evidence_summary(session['id'])}

    def session_detail(self, session_id):
        session = self.store.session(session_id)
        if not session:
            raise HTTPException(404, 'Investigation not found or summary retention expired')
        public = self.public_session(session)
        citations = self.citations(session)
        present = {e['id'] for e in self.store.evidence_by_ids(session_id, citations)}
        return {'session': public, 'evidence_summary': self.store.evidence_summary(session_id),
                'missing_citations': [eid for eid in citations if eid not in present], 'report': self.report(session)}

    def report(self, s):
        """Debrief facts, kept as separate measures rather than a score."""
        operator = self.store.operator_checks(s['id'])
        since = lambda at: round(at - s['started_at'], 1) if at else None
        first_remedy = next((a for a in s.get('recovery_attempts', []) if a.get('status') == 'succeeded'), None)
        effective = [a for a in s.get('recovery_attempts', []) if a.get('status') == 'succeeded']
        return {'detected_after_s': since(s.get('detected_at')), 'first_check_after_s': since(operator[0]['at']) if operator else None,
                'operator_checks': len(operator), 'checks_by_kind': {k: sum(1 for e in operator if e['kind'] == k) for k in sorted({e['kind'] for e in operator})},
                'help_requests': len(s.get('help', [])), 'notes': len(s.get('notes', [])),
                'recovery_attempts': len(s.get('recovery_attempts', [])),
                'ineffective_attempts': sum(1 for a in s.get('recovery_attempts', []) if a.get('status') != 'succeeded'),
                'first_effective_action_after_s': since(first_remedy['at']) if first_remedy else None,
                'effective_actions': len(effective),
                'verified_after_s': since(s.get('recovery_verified_at')), 'revealed': bool(s.get('revealed')), 'outcome': s.get('outcome')}

    def timeline(self, session_id):
        s = self.store.session(session_id)
        if not s:
            raise HTTPException(404, 'Investigation not found or summary retention expired')
        points = [{'at': e['at'], 'node': e['node'], 'kind': e['kind'], 'ok': e['body'].get('ok'), 'slow': bool(e['body'].get('slow')),
                   'ms': e['body'].get('duration_ms'), 'origin': e['body'].get('origin')}
                  for e in self.store.evidence_series(session_id, ('liveness', 'business'), 1500)]
        events = [{'at': s['started_at'], 'label': 'Investigation started'}]
        if s.get('detected_at'):
            events.append({'at': s['detected_at'], 'label': 'Failures detected'})
        events += [{'at': h['at'], 'label': 'Help: ' + h['level']} for h in s.get('help', [])]
        events += [{'at': a['at'], 'label': f"{a.get('verb', 'start')} {a['node']}: {a['status']}"} for a in s.get('recovery_attempts', [])]
        if s.get('recovery_verified_at'):
            events.append({'at': s['recovery_verified_at'], 'label': 'Recovery verified'})
        if s.get('reset'):
            events.append({'at': s['reset']['at'], 'label': 'Automatic reset' if s['reset'].get('automatic') else 'Reset requested'})
        if s.get('ended_at'):
            events.append({'at': s['ended_at'], 'label': 'Baseline verified, ended'})
        return {'session_id': session_id, 'slow_ms': SLOW_MS, 'points': points, 'events': sorted(events, key=lambda e: e['at'])}

    @staticmethod
    def citations(session):
        ids = [eid for note in [*session.get('notes', []), *session.get('help', [])]
               for eid in note.get('evidence_ids', [])]
        ids.extend(session.get('recovery_evidence_ids', []))
        ids.extend(session.get('reset_evidence_ids', []))
        return list(dict.fromkeys(ids))

    async def maintenance(self, node, verb):
        try:
            response = await self.client.post(self.nodes[node] + '/maintenance/' + verb, headers={'X-Aegis-Fleet-Token': self.fleet_token}, timeout=10)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, 'Service maintenance endpoint unavailable or rejected the operation. No success assumed.') from error

    async def mutate(self, key, node, verb):
        self.persistence_budget()
        request = json.dumps({'node': node, 'verb': verb}, sort_keys=True)
        previous = self.store.action(key)
        if previous:
            if previous['request'] != request or previous['session_id'] != (self.session['id'] if self.session else None):
                raise HTTPException(409, 'Idempotency key reused for a different operation')
            return previous['body']
        sid = self.session['id'] if self.session else None
        self.store.save_action(key, sid, request, {'status': 'running', 'node': node, 'verb': verb})
        try:
            if verb in NODE_VERBS:
                result = await self.maintenance(node, verb)
            else:
                target = await self.broker(node, 'inspect')
                result = await self.broker(node, verb, target['generation'])
            # An operation that found nothing to change is recorded honestly rather than as a success.
            status = 'unchanged' if result.get('changed') is False else 'succeeded'
            body = {'status': status, 'node': node, 'verb': verb, 'result': result}
            if status == 'unchanged':
                body['detail'] = UNCHANGED[verb]
        except HTTPException:
            body = {'status': 'unknown', 'node': node, 'verb': verb, 'detail': 'Inspect actual state before retrying; completion was not confirmed'}
        self.store.save_action(key, sid, request, body)
        return body

    async def start(self, data):
        async with self.lock:
            if self.session and self.session.get('start_key') == data.key:
                if self.session['mode'] != data.mode or (data.mode == 'practice' and (self.session['fault_target'], self.session['fault_kind']) != (data.target, data.kind)):
                    raise HTTPException(409, 'Start key reused for a different investigation')
                return self.public_session()
            if self.store.action(data.key):
                raise HTTPException(409, 'Start key already used; use a new request key')
            if self.session and self.session['status'] != 'ended':
                raise HTTPException(409, 'End/reset the current investigation before starting another')
            if self.baseline < 5 or not self.last_cycle or time.time() - self.last_cycle > 6:
                raise HTTPException(409, 'Five fresh healthy baseline cycles required')
            self.admission()
            if not self.fleet_token and (data.mode == 'challenge' or FAULTS[data.kind][0] in NODE_VERBS):
                raise HTTPException(409, 'This scenario needs AEGIS_FLEET_TOKEN. Run scripts/init_env.py, then docker compose up -d.')
            target = secrets.choice(list(self.nodes)) if data.mode == 'challenge' else data.target
            kind = secrets.choice(list(FAULTS)) if data.mode == 'challenge' else data.kind
            # Resolve both targets before recording an active scenario or changing anything.
            for node in self.nodes:
                inspected = await self.broker(node, 'inspect')
                if not inspected.get('running'):
                    self.baseline = 0
                    raise HTTPException(409, 'Fleet state changed since baseline; inspect and restore it before injection')
            self.session = {'id': uuid.uuid4().hex, 'mode': data.mode, 'status': 'observing', 'started_at': time.time(),
                            'deadline': time.time() + 900, 'fault_target': target, 'fault_kind': kind,
                            'revealed': False, 'help': [], 'notes': [], 'recovery_attempts': [], 'start_key': data.key,
                            'verification': 'Collecting measured effects', 'outcome': None}
            self.store.save(self.session)
            result = await self.mutate(data.key, target, FAULTS[kind][0])
            self.session['fault_action'] = result
            if result['status'] != 'succeeded':
                self.session['status'] = 'investigating'
                self.session['verification'] = 'Injection outcome uncertain; inspect fleet or reset'
            self.baseline = self.successes = self.failures = 0
            self.store.save(self.session)
            return self.public_session()

    async def reset_locked(self, key, automatic=False):
        s = self.session
        if not s:
            raise HTTPException(409, 'No investigation to reset')
        plan = self.reset_plan(s)
        if s.get('reset_key') == key and len(s.get('reset', {}).get('results', [])) == len(plan):
            return self.public_session()
        if s.get('reset_key') != key:
            s['reset'] = {'automatic': automatic, 'at': time.time(), 'results': []}
        s['reset_key'] = key
        s['status'] = 'resetting'
        self.store.save(s)
        results = [await self.mutate(key + '-' + node + ('' if verb == 'start' else '-' + verb), node, verb) for node, verb in plan]
        s['status'] = 'resetting'
        s.setdefault('reset', {'automatic': automatic, 'at': time.time()})['results'] = results
        s['verification'] = 'Reset requested; waiting for three fresh healthy cycles'
        self.successes = 0
        self.store.save(s)
        return self.public_session()


@asynccontextmanager
async def lifespan(app):
    path = os.getenv('AEGIS_STATE', 'state/lab.sqlite')
    with controller_ownership(path):
        async with controller_runtime(app, path):
            yield


@asynccontextmanager
async def controller_runtime(app, path):
    store = Store(path)
    async with httpx.AsyncClient(trust_env=False) as client:
        lab = Lab(store, client)
        app.state.lab = lab
        app.state.csrf = secrets.token_urlsafe(32)
        async def collect():
            next_retention = 0.0
            while True:
                try:
                    if time.monotonic() >= next_retention:
                        async with lab.lock:
                            maintenance = store.prune()
                        next_retention = time.monotonic() + (2 if maintenance['batch_full'] or maintenance['checkpoint_busy'] else 3600)
                    await lab.cycle()
                except Exception as error:
                    lab.collector_error = error.detail if isinstance(error, HTTPException) else type(error).__name__ + ': collection/persistence interrupted'
                    lab.baseline = lab.successes = 0
                await asyncio.sleep(2)
        task = asyncio.create_task(collect())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            store.db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def protect(request: Request, call_next):
    # Reject browser cross-origin requests and DNS-rebinding hosts before issuing session tokens.
    origin = os.getenv('AEGIS_ORIGIN', 'http://localhost:8787')
    allowed_hosts = {'localhost:8787', '127.0.0.1:8787'}
    if request.headers.get('host') not in allowed_hosts:
        return JSONResponse({'detail': 'Untrusted host'}, status_code=403)
    if request.method not in ('GET', 'HEAD'):
        origins = {origin} if os.getenv('AEGIS_ORIGIN') else {'http://localhost:8787', 'http://127.0.0.1:8787'}
        if request.headers.get('origin') not in origins or not hmac.compare_digest(request.headers.get('x-aegis-token', ''), getattr(app.state, 'csrf', '!')):
            return JSONResponse({'detail': 'Operator session or origin invalid; refresh the page'}, status_code=403)
        if len(await request.body()) > 16384:
            return JSONResponse({'detail': 'Request too large'}, status_code=413)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
    return response


@app.get('/api/state')
async def state():
    return {**app.state.lab.state(), 'token': app.state.csrf}


@app.post('/api/sessions')
async def start(data: Start):
    return await app.state.lab.start(data)


@app.post('/api/check')
async def check(data: Check):
    async with app.state.lab.lock:
        return await app.state.lab.check(data.node, data.kind)


def active(session_id):
    lab = app.state.lab
    if not lab.session or lab.session['status'] == 'ended' or lab.session['id'] != session_id:
        raise HTTPException(409, 'This investigation is no longer active. Refresh before acting.')
    return lab


@app.post('/api/recover')
async def recover(data: Recover):
    lab = app.state.lab
    async with lab.lock:
        active(data.session_id)
        if lab.session['status'] == 'resetting':
            raise HTTPException(409, 'Reset is in progress. Wait for verification or retry reset.')
        if len(lab.session['recovery_attempts']) >= 100 and not any(a['key'] == data.key for a in lab.session['recovery_attempts']):
            raise HTTPException(409, 'Recovery-attempt limit reached; reset and begin a new investigation')
        result = await lab.mutate(data.key, data.node, data.verb)
        if not any(a['key'] == data.key for a in lab.session['recovery_attempts']):
            lab.session['recovery_attempts'].append({'key': data.key, 'at': time.time(), **result})
            lab.session['status'] = 'verifying'
            lab.session['verification'] = 'Waiting for fresh measured recovery; an accepted action is not proof'
            lab.successes = 0
        lab.store.save(lab.session)
        return result


@app.post('/api/reset')
async def reset(data: Action):
    lab = app.state.lab
    async with lab.lock:
        active(data.session_id)
        return await lab.reset_locked(data.key)


@app.post('/api/extend')
async def extend(data: Action):
    lab = app.state.lab
    async with lab.lock:
        active(data.session_id)
        if lab.session['status'] in ('resetting', 'recovered'):
            raise HTTPException(409, 'The timer no longer applies')
        if time.time() > lab.session['deadline'] + 60:
            raise HTTPException(409, 'Continuation grace period ended; reset is required')
        lab.admission()
        if len(lab.session.setdefault('extensions', [])) >= 100 and data.key not in lab.session['extensions']:
            raise HTTPException(409, 'Extension limit reached; export and reset this investigation')
        if data.key not in lab.session['extensions']:
            lab.session['deadline'] = max(time.time(), lab.session['deadline']) + 900
            lab.session['extensions'].append(data.key)
        lab.store.save(lab.session)
        return lab.public_session()


@app.post('/api/reveal')
async def reveal(data: Action):
    lab = app.state.lab
    async with lab.lock:
        active(data.session_id)
        lab.session['revealed'] = True
        lab.session['revealed_at'] = time.time()
        lab.store.save(lab.session)
        return lab.public_session()


@app.post('/api/note')
async def note(data: Note):
    lab = app.state.lab
    async with lab.lock:
        active(data.session_id)
        ids = {e['id'] for e in lab.store.evidence_by_ids(lab.session['id'], data.evidence_ids)}
        if not set(data.evidence_ids) <= ids:
            raise HTTPException(422, 'Evidence must belong to this investigation and still be retained')
        if len(lab.session['notes']) >= 100:
            raise HTTPException(409, 'This investigation has reached its 100-note limit; export before starting another')
        lab.session['notes'].append({'at': time.time(), **data.model_dump(exclude={'session_id'})})
        lab.store.save(lab.session)
        return {'saved': True}


@app.post('/api/help')
async def help_request(data: Help):
    lab = app.state.lab
    async with lab.lock:
        active(data.session_id)
        if len(lab.session['help']) >= 200:
            raise HTTPException(409, 'This investigation has reached its assistance-record limit')
        guidance = explain({key: value for key,value in lab.latest.items() if value['session_id'] == lab.session['id']}, data.level)
        lab.session['help'].append({'at': time.time(), 'level': data.level,
                                    'evidence_ids': guidance['evidence_ids'], 'message': guidance['message']})
        lab.store.save(lab.session)
        return {**guidance, 'evidence': lab.store.evidence_by_ids(lab.session['id'], guidance['evidence_ids'])}


@app.get('/api/investigations/{session_id}')
async def investigation(session_id: str):
    return app.state.lab.session_detail(session_id)


@app.get('/api/investigations/{session_id}/timeline')
async def investigation_timeline(session_id: str):
    return app.state.lab.timeline(session_id)


@app.get('/api/investigations/{session_id}/evidence')
async def investigation_evidence(session_id: str, limit: int = Query(default=50, ge=1, le=100),
                                 before: str | None = Query(default=None, max_length=80),
                                 node: Literal['gateway-a', 'catalog-b'] | None = None,
                                 kind: CheckKind | None = None):
    lab = app.state.lab
    lab.session_detail(session_id)
    try:
        return lab.store.evidence_page(session_id, limit, before, node, kind)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get('/api/investigations/{session_id}/evidence/{evidence_id}')
async def investigation_observation(session_id: str, evidence_id: str):
    lab = app.state.lab
    lab.session_detail(session_id)
    items = lab.store.evidence_by_ids(session_id, [evidence_id])
    if not items:
        raise HTTPException(404, 'Observation expired or does not belong to this investigation')
    return items[0]


@app.post('/api/maintenance')
async def maintenance():
    lab = app.state.lab
    async with lab.lock:
        return lab.store.prune()


@app.get('/api/export')
async def export(session_id: str | None = Query(default=None, max_length=80)):
    lab = app.state.lab
    sid = session_id or (lab.session['id'] if lab.session else None)
    if not sid:
        raise HTTPException(409, 'Select an investigation from history to export')
    detail = lab.session_detail(sid)
    evidence = lab.store.evidence(sid, 500)
    # Preserve cited observations even when newer automatic samples fill the recent window.
    cited_ids = lab.citations(detail['session'])
    cited = lab.store.evidence_by_ids(sid, cited_ids)
    # Bound exports to 8 MiB while prioritising citations, and explicitly report omissions.
    selected, size = [], len(json.dumps(detail).encode())
    cited_set = {c['id'] for c in cited}
    for record in [*cited, *(e for e in evidence if e['id'] not in cited_set)]:
        encoded_size = len(json.dumps(record).encode())
        if size + encoded_size > 8 * 1024**2 - 4096:
            continue
        selected.append(record)
        size += encoded_size
    omitted = detail['evidence_summary']['count'] - len(selected)
    return JSONResponse({**detail, 'evidence': selected, 'omitted_evidence_count': omitted,
                         'export_note': 'Cited observations plus up to 500 recent records, bounded to 8 MiB. Missing citations have expired; omitted records may still be browsed. Ground truth withheld until reveal/end.'},
                        headers={'Content-Disposition': 'attachment; filename="aegis-investigation.json"'})


@app.get('/')
async def index():
    return FileResponse(Path(__file__).resolve().parents[1] / 'web' / 'index.html')


app.mount('/static', StaticFiles(directory=Path(__file__).resolve().parents[1] / 'web'), name='static')
