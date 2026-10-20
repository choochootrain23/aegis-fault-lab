'use strict';
// Browser-only stand-in for the AegisRack controller, broker and fleet.
// It mirrors the rules in aegis/controller.py and aegis/guidance.py so the
// unmodified web UI can run on a static host. Every measurement it returns is
// generated in this tab; nothing here touches a container or a network service.
(() => {
const NODES = ['gateway-a', 'catalog-b'];
const START_DELAY = 1.2;   // seconds a started service takes to answer, roughly the real lab
const MAX_EVIDENCE = 6000; // keeps a long-open tab bounded
let skew = 0;
const now = () => Date.now() / 1000 + skew;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const hex = length => [...crypto.getRandomValues(new Uint8Array(length / 2))].map(b => b.toString(16).padStart(2, '0')).join('');
const between = (low, high) => Math.round((low + Math.random() * (high - low)) * 10) / 10;
const clone = value => JSON.parse(JSON.stringify(value));

class HttpError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

// ---- Simulated fleet: two services, their scratch disks, runaway jobs and logs ----
// Latencies under a runaway job follow the real lab's measurements (catalogue median ~400-600 ms).
const SLOW_MS = 300;
const DISK = 8 * 1024 * 1024;
const fleet = Object.fromEntries(NODES.map(id => [id, {container: hex(64), process: 'sim-' + crypto.randomUUID(), running: true, readyAt: 0,
  busy: false, full: false, drifted: false, periods: 4000, throttled: 0, logs: []}]));
const answering = id => fleet[id].running && now() >= fleet[id].readyAt;
const catalogue = () => ({items: [{sku: 'LAB-001', name: 'Synthetic component', quantity: 12}], generation: fleet['catalog-b'].process});
const reply = (result, duration) => ({ok: true, http_status: 200, result, duration_ms: duration});
const log = (node, entry) => { const logs = fleet[node].logs; logs.push({at: now(), generation: fleet[node].process, ...entry}); if (logs.length > 200) logs.shift(); };
const busyMs = () => Math.round(Math.min(1600, 110 + Math.random() ** 1.6 * 1100) * 10) / 10;
const diskFull = node => ({ok: false, error: 'OSError', errno: 'ENOSPC', detail: 'No space left on device', path: 'receipt write', generation: fleet[node].process});
// Runtime settings as aegis/fleet.py reports them; drift overrides one, as a bad hot reload would.
const UPSTREAM = 'http://catalog-b:8000';
const DEPLOYED = {'gateway-a': {upstream: UPSTREAM, upstream_timeout_s: 1.0}, 'catalog-b': {catalogue_source: 'builtin:lab-v1', receipt_ring: 40}};
const DRIFT = {'gateway-a': ['upstream', 'http://catalog-b:8080'], 'catalog-b': ['catalogue_source', 'file:/tmp/catalogue-v2.json']};
const settings = node => Object.entries(DEPLOYED[node]).map(([key, deployed]) => {
  const value = fleet[node].drifted && DRIFT[node][0] === key ? DRIFT[node][1] : deployed;
  return {key, value, deployed, drifted: value !== deployed};
});
const sourceMissing = node => ({ok: false, error: 'FileNotFoundError', detail: 'catalogue source unavailable', source: DRIFT[node][1], generation: fleet[node].process});

// One traced business request, shaped like /diagnostics/trace.
function trace(node) {
  const n = fleet[node], spans = [];
  let clock = between(0.1, 0.4);
  const span = (name, ms, ok, detail = {}) => { spans.push({name, start_ms: Math.round(clock * 10) / 10, ms, ok, ...detail}); clock += ms; return ok; };
  const own = (low, high) => n.busy ? Math.round(busyMs() * 0.4 * 10) / 10 : between(low, high);
  let ok;
  if (node === 'gateway-a') {
    const [address] = (n.drifted ? DRIFT[node][1] : UPSTREAM).split('//').slice(-1), name = `upstream ${address}/catalog`, cat = fleet['catalog-b'];
    if (n.drifted || !answering('catalog-b')) ok = span(name, between(0.8, 3), false, {error: 'ConnectError'});
    else if (cat.drifted) ok = span(name, between(2, 6), false, {status: 500});
    else if (cat.full) ok = span(name, between(2, 6), false, {status: 507});
    else {
      const ms = cat.busy ? busyMs() : between(1.5, 6);
      ok = ms > 1000 ? span(name, 1000, false, {error: 'ReadTimeout'}) : span(name, ms, true, {status: 200});
    }
  } else {
    ok = n.drifted ? span('catalogue load', own(0.1, 0.5), false, {source: DRIFT[node][1], error: 'FileNotFoundError'})
      : span('catalogue load', own(0.1, 0.5), true, {source: 'builtin:lab-v1'});
  }
  if (ok) ok = n.full ? span('receipt write', own(0.1, 0.4), false, {error: 'ENOSPC'}) : span('receipt write', own(0.2, 1.5), true);
  const handler = Math.round((clock + between(0.1, 0.5)) * 10) / 10;
  return reply({request_ok: ok, failed_at: spans.find(s => !s.ok)?.name ?? null, spans, handler_ms: handler, generation: n.process},
    Math.round((handler + (n.busy ? busyMs() * 0.6 : between(1.5, 5))) * 10) / 10);
}

function probe(node, kind) {
  const n = fleet[node];
  if (kind === 'service') {
    return {ok: true, target: node, generation: n.container, running: n.running,
            status: n.running ? 'running' : 'exited', exit_code: 0, duration_ms: between(9, 30)};
  }
  if (kind === 'resources') {
    const base = {ok: true, target: node, generation: n.container, running: n.running, status: n.running ? 'running' : 'exited', exit_code: 0, cpu_limit: 0.25};
    if (!n.running) return {...base, duration_ms: between(9, 30)};
    const cores = Math.round((n.busy ? 0.24 + Math.random() * 0.01 : 0.004 + Math.random() * 0.016) * 1000) / 1000;
    n.periods += 10; if (n.busy) n.throttled += 9;
    return {...base, cpu_cores: cores, cpu_pct_of_limit: Math.round(cores / 0.25 * 1000) / 10, periods: n.periods, throttled_periods: n.throttled,
            throttled_ms: Math.round(n.throttled * 75 * 10) / 10, memory_mib: between(38, 46), memory_limit_mib: 192, duration_ms: between(1000, 1250)};
  }
  if (!answering(node)) return {ok: false, error: 'ConnectError', detail: 'Check unavailable or failed', duration_ms: between(0.8, 4)};
  const role = node === 'gateway-a' ? 'gateway' : 'catalog';
  const quick = (low, high) => n.busy ? busyMs() : between(low, high);
  if (kind === 'liveness') return reply({live: true, role, generation: n.process}, quick(3, 13));
  if (kind === 'logs') return reply({entries: n.logs.slice(-200), bounded_to: 200, generation: n.process, truncated: false, omitted_retained_entries: 0}, quick(3, 9));
  if (kind === 'disk') {
    const free = n.full ? 0 : DISK - 40 * 4096;
    const largest = n.full ? [{path: 'aegis-fill.bin', bytes: DISK - 40 * 4096}, {path: 'receipts/00000412.json', bytes: 230}] : [{path: 'receipts/00000412.json', bytes: 230}];
    return reply({mount: '/tmp', total_bytes: DISK, free_bytes: free, used_pct: Math.round((DISK - free) / DISK * 1000) / 10, largest, file_count: 40 + (n.full ? 1 : 0), generation: n.process}, quick(3, 9));
  }
  if (kind === 'config') return reply({role, settings: settings(node), drifted: settings(node).filter(s => s.drifted).map(s => s.key), generation: n.process}, quick(3, 9));
  if (kind === 'trace') return trace(node);
  if (role === 'catalog') {
    if (kind === 'readiness') return reply({ok: true, generation: n.process}, quick(4, 10));
    if (n.drifted) { const failure = {...sourceMissing(node), duration_ms: quick(2, 6)}; log(node, failure); return {ok: false, http_status: 500, result: failure, duration_ms: failure.duration_ms}; }
    if (n.full) { const failure = diskFull(node); log(node, failure); return {ok: false, http_status: 507, result: failure, duration_ms: quick(4, 10)}; }
    return reply(catalogue(), quick(5, 12));
  }
  // Gateway readiness and business requests call the catalogue dependency.
  const correlation = crypto.randomUUID(), cat = fleet['catalog-b'];
  let result, status = 503;
  const upstream = answering('catalog-b') ? (cat.busy ? busyMs() : between(1.5, 6)) : between(1, 5);
  if (n.drifted || !answering('catalog-b')) result = {ok: false, dependency: 'catalog-b', correlation, error: 'ConnectError'};
  else if (cat.drifted) { result = {ok: false, dependency: 'catalog-b', correlation, error: 'HTTPStatusError', upstream_status: 500}; log('catalog-b', sourceMissing('catalog-b')); }
  else if (cat.full) { result = {ok: false, dependency: 'catalog-b', correlation, error: 'HTTPStatusError', upstream_status: 507}; log('catalog-b', diskFull('catalog-b')); }
  else if (upstream > 1000) result = {ok: false, dependency: 'catalog-b', correlation, error: 'ReadTimeout'};
  else { result = {ok: true, dependency: 'catalog-b', correlation, catalog: catalogue()}; status = 200; }
  Object.assign(result, {generation: n.process, duration_ms: Math.min(upstream, 1000)});
  const {catalog: _, ...logged} = result;
  log(node, logged);
  const total = Math.round((result.duration_ms + (n.busy ? busyMs() : between(2, 9))) * 10) / 10;
  if (status === 200 && n.full) { const failure = diskFull(node); log(node, failure); return {ok: false, http_status: 507, result: failure, duration_ms: total}; }
  return {ok: status === 200, http_status: status, result, duration_ms: total};
}

// ---- Simulated store ----
const evidence = [];
const sessions = new Map();
const actions = new Map();

function record(sessionId, node, kind, body) {
  const item = {id: hex(32), session_id: sessionId, at: now(), node, kind, body};
  evidence.push(item);
  if (evidence.length > MAX_EVIDENCE) {
    const index = evidence.findIndex(e => !e.session_id || sessions.get(e.session_id)?.status === 'ended');
    if (index >= 0) evidence.splice(index, 1);
  }
  return item;
}
const newestFirst = list => [...list].sort((a, b) => b.at - a.at || (b.id < a.id ? -1 : 1));
const sessionEvidence = (sid, limit) => newestFirst(evidence.filter(e => e.session_id === sid)).slice(0, limit);
const evidenceByIds = (sid, ids) => newestFirst(evidence.filter(e => e.session_id === sid && ids.includes(e.id)));
function evidenceSummary(sid) {
  const items = evidence.filter(e => e.session_id === sid);
  return {count: items.length, oldest: items.length ? Math.min(...items.map(e => e.at)) : null, newest: items.length ? Math.max(...items.map(e => e.at)) : null};
}
function storage() {
  const live = evidence.length * 900 + sessions.size * 4096 + 98304;
  return {live_bytes: live, allocated_bytes: live, wal_bytes: 0, live_limit_bytes: 256 * 1024 ** 2, wal_limit_bytes: 128 * 1024 ** 2};
}

// ---- Simulated controller, following aegis/controller.py ----
const lab = {latest: new Map(), baseline: 0, successes: 0, failures: 0, healthy: [], lastCycle: null, session: null};

function check(node, kind, origin = 'operator') {
  const body = {...probe(node, kind), origin, simulated: true};
  if (body.ok && ['liveness', 'readiness', 'business'].includes(kind) && body.duration_ms > SLOW_MS) body.slow = true;
  if (kind === 'trace' && body.result?.handler_ms != null) body.outside_handler_ms = Math.round(Math.max(0, body.duration_ms - body.result.handler_ms) * 10) / 10;
  if (kind === 'service' || kind === 'resources') body.generation_scope = 'container';
  else if (body.result?.generation) Object.assign(body, {process_generation: body.result.generation, generation_scope: 'process'});
  const sid = lab.session && lab.session.status !== 'ended' ? lab.session.id : null;
  const item = record(sid, node, kind, body);
  lab.latest.set(node + '|' + kind, item);
  return item;
}

function cycle() {
  const results = NODES.flatMap(n => ['liveness', 'business'].map(k => check(n, k, 'collector')));
  const good = results.every(e => e.body.ok && !e.body.slow);
  lab.baseline = good ? lab.baseline + 1 : 0;
  lab.successes = good ? lab.successes + 1 : 0;
  if (lab.successes <= 1) lab.healthy = [];
  if (good) lab.healthy = [...lab.healthy, results.map(e => e.id)].slice(-3);
  lab.failures = good ? 0 : lab.failures + 1;
  lab.lastCycle = now();
  const s = lab.session;
  if (!s || s.status === 'ended') return;
  if (lab.failures >= 3 && s.status === 'observing') {
    s.status = 'investigating';
    s.detected_at = now();
    s.verification = 'Repeated failures observed; investigate the evidence';
  }
  if (['verifying', 'resetting'].includes(s.status) && lab.successes >= 3) {
    if (s.status === 'resetting') {
      Object.assign(s, {status: 'ended', ended_at: now(), outcome: s.outcome || 'reset',
        closure: s.reset?.automatic ? 'automatic_reset' : 'operator_reset',
        verification: 'Baseline verified; this is not a targeted recovery', reset_evidence_ids: lab.healthy.flat()});
    } else {
      Object.assign(s, {status: 'recovered', outcome: 'verified_recovery', recovery_verified_at: now(),
        verification: 'Three consecutive healthy fleet and business checks', recovery_evidence_ids: lab.healthy.flat()});
    }
    s.verified_at = now();
  } else if (s.status === 'verifying' && !good) {
    s.verification = 'Recovery has not restored all affected checks';
  }
  if (!['ended', 'recovered', 'resetting'].includes(s.status) && now() >= s.deadline + 60) resetLocked('expiry-' + s.id, true);
}

const HIDDEN = ['fault_target', 'fault_kind', 'fault_action', 'start_key', 'reset_key', 'extensions'];
function publicSession(s = lab.session) {
  if (!s) return null;
  const result = clone(Object.fromEntries(Object.entries(s).filter(([k]) => !HIDDEN.includes(k))));
  if (s.mode === 'practice' || s.revealed || s.status === 'ended') {
    result.scenario = {kind: s.fault_kind, target: s.fault_target};
    if (s.revealed || s.status === 'ended') result.debrief = debrief(s);
  }
  return result;
}

function state() {
  const t = now();
  const nodes = NODES.map(id => {
    const live = lab.latest.get(id + '|liveness'), business = lab.latest.get(id + '|business');
    const fresh = live && business && [live, business].every(e => t - e.at >= 0 && t - e.at < 6);
    const fast = fresh && !live.body.slow && !business.body.slow;
    const health = !fresh ? 'unknown' : !live.body.ok ? 'unavailable' : business.body.ok && fast ? 'healthy' : 'degraded';
    return {id, role: id === 'gateway-a' ? 'gateway' : 'catalogue', health, live: live || null, business: business || null};
  });
  const history = [...sessions.values()].sort((a, b) => b.started_at - a.started_at).slice(0, 20).map(s => {
    const p = publicSession(s);
    return {id: p.id, mode: p.mode, status: p.status, started_at: p.started_at, outcome: p.outcome ?? null, closure: p.closure ?? null,
      revealed: p.revealed, recovery_verified_at: p.recovery_verified_at ?? null, notes_count: p.notes.length, help_count: p.help.length,
      evidence: evidenceSummary(s.id)};
  });
  return {nodes, session: publicSession(), baseline_cycles: lab.baseline, healthy_streak: lab.successes, collector_error: null, last_cycle: lab.lastCycle,
    evidence: lab.session ? sessionEvidence(lab.session.id, 60) : newestFirst(evidence.filter(e => !e.session_id)).slice(0, 60),
    ai: {available: false, reason: 'AI integration is a later milestone; manual investigation is fully available.'},
    now: t, history, storage: storage(), token: 'simulator',
    retention: {raw_hours: 24, summary_days: 30, warning: 'Export supporting evidence before 24 hours. Older references may become unavailable.'}};
}

// Docker-equivalent effects. Starting a running service or clearing a disk that is not full
// changes nothing and says so, as the real broker and services do; a stopped service cannot confirm maintenance.
const FAULTS = {'service-unavailable': ['stop', 'start'], 'cpu-starvation': ['burn', 'calm'], 'disk-full': ['fill', 'clear'], 'config-drift': ['drift', 'restore']};
const UNCHANGED = {start: 'Already running; nothing changed', stop: 'Already stopped; nothing changed', calm: 'No background job running; nothing changed',
  burn: 'A background job was already running; nothing changed', clear: 'No filler file present; nothing changed', fill: 'Nothing changed',
  restore: 'Running config already matches the deployed settings; nothing changed', drift: 'Setting already overridden; nothing changed'};
const DEBRIEFS = {
  'service-unavailable': 'The scenario requested stopping {target}. When that succeeds, its container state reads stopped and its own probes fail. When the catalogue stops, the gateway stays live but its business requests fail, so the failure spreads; when the gateway stops, the catalogue stays healthy, so it does not. Starting the stopped service restores the request path.',
  'cpu-starvation': "The scenario requested a runaway background job inside {target}. When that succeeds, the service keeps running and answering, but slowly: the job uses the container's whole 0.25-CPU limit, the kernel throttles it, and requests queue behind it, crossing the 300 ms threshold. Its log records the job starting. A slow catalogue makes the gateway slow, and sometimes time out, too. Stopping the job returns normal latency.",
  'disk-full': "The scenario requested filling {target}'s 8 MiB /tmp with one large file. When that succeeds, the process stays live, but every business request must write a small receipt, so writes fail with ENOSPC (HTTP 507). A full catalogue fails the gateway's requests too; a full gateway fails only its own. Disk usage shows 100% and the filler as the largest file. Clearing it restores writes.",
  'config-drift': "The scenario requested a bad runtime configuration reload on {target}. When that succeeds, the process stays live and its container keeps running, so liveness, container state, CPU and disk all look normal. On the gateway, the upstream address points at a port nothing listens on, so every business request fails to connect, which looks like a stopped catalogue until the catalogue is checked directly. On the catalogue, the data source points at a dataset that was never deployed, so its business requests fail with HTTP 500 and the gateway's fail with them. The running config shows the drifted setting next to its deployed value, and the log records the reload. Restoring the deployed config fixes it.",
};
const debrief = s => DEBRIEFS[s.fault_kind].replace('{target}', s.fault_target) + ' Injection outcome: ' + (s.fault_action?.status || 'not recorded') + '. The action record explains the exercise but was not evidence available during the investigation. Other evidence-supported investigation orders are valid.';

function apply(node, verb) {
  const target = fleet[node];
  if (verb === 'start' || verb === 'stop') {
    if ((verb === 'start') === target.running) return false;
    target.running = verb === 'start';
    if (target.running) Object.assign(target, {readyAt: now() + START_DELAY, process: 'sim-' + crypto.randomUUID(), busy: false, full: false, drifted: false, logs: []});
    return true;
  }
  if (!target.running) return null;  // maintenance endpoint unreachable
  const field = {burn: 'busy', calm: 'busy', fill: 'full', clear: 'full', drift: 'drifted', restore: 'drifted'}[verb];
  const on = ['burn', 'fill', 'drift'].includes(verb);
  if (target[field] === on) return false;
  target[field] = on;
  if (field === 'drifted') log(node, on ? {ok: true, event: 'configuration reloaded', changed: {[DRIFT[node][0]]: DRIFT[node][1]}} : {ok: true, event: 'configuration restored', keys: [DRIFT[node][0]]});
  if (field === 'busy') log(node, {ok: true, event: on ? 'background job started' : 'background job stopped', job: 'catalogue reindex', ...(on ? {threads: 3} : {})});
  return true;
}

function mutate(key, node, verb) {
  const request = JSON.stringify({node, verb});
  const previous = actions.get(key);
  if (previous) {
    if (previous.request !== request || previous.session_id !== (lab.session?.id ?? null)) throw new HttpError(409, 'Idempotency key reused for a different operation');
    return previous.body;
  }
  const changed = apply(node, verb);
  const body = changed === null ? {status: 'unknown', node, verb, detail: 'Inspect actual state before retrying; completion was not confirmed'}
    : {status: changed ? 'succeeded' : 'unchanged', node, verb, result: {target: node, changed}, ...(changed ? {} : {detail: UNCHANGED[verb]})};
  actions.set(key, {request, session_id: lab.session?.id ?? null, at: now(), body});
  return body;
}

const resetPlan = s => [...NODES.map(n => [n, 'start']), ...(FAULTS[s.fault_kind][1] !== 'start' ? [[s.fault_target, FAULTS[s.fault_kind][1]]] : [])];

function save(s) { sessions.set(s.id, s); }

function start(data) {
  if (lab.session && lab.session.start_key === data.key) {
    if (lab.session.mode !== data.mode || (data.mode === 'practice' && (lab.session.fault_target !== data.target || lab.session.fault_kind !== data.kind))) throw new HttpError(409, 'Start key reused for a different investigation');
    return publicSession();
  }
  if (actions.has(data.key)) throw new HttpError(409, 'Start key already used; use a new request key');
  if (lab.session && lab.session.status !== 'ended') throw new HttpError(409, 'End/reset the current investigation before starting another');
  if (lab.baseline < 5 || !lab.lastCycle || now() - lab.lastCycle > 6) throw new HttpError(409, 'Five fresh healthy baseline cycles required');
  const pick = list => list[crypto.getRandomValues(new Uint8Array(1))[0] % list.length];
  const target = data.mode === 'challenge' ? pick(NODES) : data.target;
  const kind = data.mode === 'challenge' ? pick(Object.keys(FAULTS)) : data.kind;
  if (!NODES.every(n => fleet[n].running)) {
    lab.baseline = 0;
    throw new HttpError(409, 'Fleet state changed since baseline; inspect and restore it before injection');
  }
  lab.session = {id: hex(32), mode: data.mode, status: 'observing', started_at: now(), deadline: now() + 900,
    fault_target: target, fault_kind: kind, revealed: false, help: [], notes: [], recovery_attempts: [],
    start_key: data.key, verification: 'Collecting measured effects', outcome: null};
  save(lab.session);
  lab.session.fault_action = mutate(data.key, target, FAULTS[kind][0]);
  lab.baseline = lab.successes = lab.failures = 0;
  return publicSession();
}

function resetLocked(key, automatic = false) {
  const s = lab.session;
  if (!s) throw new HttpError(409, 'No investigation to reset');
  const plan = resetPlan(s);
  if (s.reset_key === key && (s.reset?.results || []).length === plan.length) return publicSession();
  if (s.reset_key !== key) s.reset = {automatic, at: now(), results: []};
  s.reset_key = key;
  s.status = 'resetting';
  s.reset.results = plan.map(([node, verb]) => mutate(key + '-' + node + (verb === 'start' ? '' : '-' + verb), node, verb));
  s.verification = 'Reset requested; waiting for three fresh healthy cycles';
  lab.successes = 0;
  return publicSession();
}

function active(sessionId) {
  if (!lab.session || lab.session.status === 'ended' || lab.session.id !== sessionId) throw new HttpError(409, 'This investigation is no longer active. Refresh before acting.');
  return lab.session;
}

// Port of aegis/guidance.py: fresh observations only, never the fault record.
function explain(level) {
  const t = now(), sid = lab.session.id, inspections = ['service', 'resources', 'disk', 'config', 'trace'];
  const fresh = new Map([...lab.latest].filter(([key, e]) => e.session_id === sid && t - e.at >= 0 && t - e.at <= (inspections.includes(key.split('|')[1]) ? 30 : 6)));
  let ids = [], message = 'There is not enough fresh evidence yet. Start with a liveness check, then compare it with a business request.';
  let suggested = {node: 'gateway-a', kind: 'liveness'};
  const live = fresh.get('gateway-a|liveness'), business = fresh.get('gateway-a|business');
  if (live && business) {
    ids = [live.id, business.id];
    const result = business.body.result || {};
    if (live.body.ok && !business.body.ok && (business.body.http_status === 507 || result.errno === 'ENOSPC')) {
      message = "The gateway is live, but its business request failed with HTTP 507 (insufficient storage) from its own receipt write. Check the gateway's disk usage.";
      suggested = {node: 'gateway-a', kind: 'disk'};
    } else if (live.body.ok && !business.body.ok && result.upstream_status === 507) {
      message = 'The gateway is live, but the catalogue answered its request with HTTP 507 (insufficient storage). The catalogue is reachable yet cannot write; check its disk usage.';
      suggested = {node: 'catalog-b', kind: 'disk'};
    } else if (live.body.ok && !business.body.ok && result.upstream_status === 500) {
      message = 'The gateway is live, but the catalogue answered its request with HTTP 500. The catalogue is reachable yet failing internally; compare its logs and running config with what was deployed.';
      suggested = {node: 'catalog-b', kind: 'config'};
    } else if (live.body.ok && !live.body.slow && !business.body.ok && fresh.get('catalog-b|business')?.body.ok && !fresh.get('catalog-b|business').body.slow) {
      ids.push(fresh.get('catalog-b|business').id);
      message = "The gateway is live but its business request fails, while the catalogue answers its own business requests quickly. The catalogue works, so the break is on the gateway's path to it: trace a gateway request and compare the gateway's running config with its deployed settings.";
      suggested = {node: 'gateway-a', kind: 'config'};
    } else if (live.body.ok && !business.body.ok) {
      message = 'The gateway responds to liveness checks but its business request fails. Its own process is reachable; inspect the catalogue dependency before concluding the gateway is down.';
      suggested = {node: 'catalog-b', kind: 'service'};
    } else if (!live.body.ok) {
      message = 'The gateway liveness check failed. This does not by itself distinguish a stopped process from a connectivity problem. Inspect its container state and compare the catalogue independently.';
      suggested = {node: 'gateway-a', kind: 'service'};
    } else if (live.body.slow || business.body.slow) {
      const slowNode = live.body.slow ? 'gateway-a' : 'catalog-b';
      message = `Gateway requests succeed but slowly (${business.body.duration_ms} ms, above ${SLOW_MS} ms). ` + (slowNode === 'gateway-a'
        ? 'The gateway is slow even to liveness checks, so look at its own resources.'
        : "Its liveness is quick, so the delay is probably in the catalogue it depends on; compare the catalogue's latency and resource usage.");
      suggested = {node: slowNode, kind: 'resources'};
    } else if (business.body.ok) {
      message = 'Recent gateway liveness and business checks succeeded. These observations do not establish an active failure; compare the incident timeline before changing anything.';
      suggested = {node: 'catalog-b', kind: 'liveness'};
    }
  }
  if (level === 'action') {
    const found = remedyFor(fresh);
    if (found) [ids, message, suggested] = found;
    else message += ' No fresh inspection yet supports a specific recovery action. Gather the suggested check first.';
  } else if (level === 'hint') {
    message += ' Compare process state, latency, storage and running config with request behaviour; one failed check is not a complete diagnosis.';
  }
  return {level, message, evidence_ids: ids, suggested_check: suggested, source: 'deterministic_observation_rules', observed_at: t};
}

function remedyFor(fresh) {
  const candidates = [], contradicted = [];
  for (const [key, record] of fresh) {
    const [node, kind] = key.split('|'), body = record.body;
    if (!body.ok) continue;
    let action;
    if (kind === 'service' && body.running === false) action = `The recent service-state observation reports ${node} stopped. You can try starting that selected service, then verify fresh business requests. This does not establish why it stopped.`;
    else if (kind === 'resources' && (body.cpu_pct_of_limit || 0) >= 80) action = `The resource check shows ${node} using ${body.cpu_pct_of_limit}% of its ${body.cpu_limit}-CPU limit, so the kernel is throttling it. Its logs may show what started; you can try stopping the runaway job, then verify latency returns below ${SLOW_MS} ms.`;
    else if (kind === 'disk' && body.result?.free_bytes != null && body.result.free_bytes < 64 * 1024) action = `Disk usage shows ${node}'s /tmp full, mostly ${body.result.largest?.[0]?.path || 'one file'}. You can try clearing its temporary files, then verify business requests write receipts again.`;
    else if (kind === 'config' && body.result?.drifted?.length) {
      const setting = body.result.settings.find(s => s.drifted);
      action = `The running config on ${node} differs from its deployed settings: ${setting.key} is ${setting.value} but ${setting.deployed} was deployed. You can try restoring the deployed config, then verify business requests succeed again.`;
    } else continue;
    const newer = kind === 'service' ? [...fresh].filter(([k, e]) => e.node === node && /\|(liveness|business)$/.test(k) && e.at > record.at && e.body.ok && !e.body.slow).map(([, e]) => e) : [];
    if (newer.length) contradicted.push([record, newer.reduce((a, b) => (b.at > a.at ? b : a))]);
    else candidates.push([record, action]);
  }
  if (candidates.length) {
    const [record, action] = candidates.reduce((a, b) => (b[0].at > a[0].at ? b : a));
    return [[record.id], action, {node: record.node, kind: record.kind}];
  }
  if (contradicted.length) {
    const [old, newer] = contradicted.reduce((a, b) => (b[1].at > a[1].at ? b : a));
    return [[old.id, newer.id], `The stopped-state observation for ${old.node} is older than a successful check from that node. Its state may have changed. Inspect its container state again before choosing a recovery action.`, {node: old.node, kind: 'service'}];
  }
  return null;
}

function citations(s) {
  const ids = [...s.notes, ...s.help].flatMap(n => n.evidence_ids || []);
  return [...new Set([...ids, ...(s.recovery_evidence_ids || []), ...(s.reset_evidence_ids || [])])];
}
function sessionDetail(id) {
  const s = sessions.get(id);
  if (!s) throw new HttpError(404, 'Investigation not found or summary retention expired');
  const present = new Set(evidenceByIds(id, citations(s)).map(e => e.id));
  return {session: publicSession(s), evidence_summary: evidenceSummary(id), missing_citations: citations(s).filter(e => !present.has(e)), report: report(s)};
}

function report(s) {
  const operator = evidence.filter(e => e.session_id === s.id && e.body.origin === 'operator').sort((a, b) => a.at - b.at);
  const since = at => (at ? Math.round((at - s.started_at) * 10) / 10 : null);
  const attempts = s.recovery_attempts || [], effective = attempts.filter(a => a.status === 'succeeded');
  const byKind = {};
  for (const e of operator) byKind[e.kind] = (byKind[e.kind] || 0) + 1;
  return {detected_after_s: since(s.detected_at), first_check_after_s: operator.length ? since(operator[0].at) : null, operator_checks: operator.length,
    checks_by_kind: Object.fromEntries(Object.entries(byKind).sort()), help_requests: s.help.length, notes: s.notes.length, recovery_attempts: attempts.length,
    ineffective_attempts: attempts.length - effective.length, first_effective_action_after_s: effective.length ? since(effective[0].at) : null,
    effective_actions: effective.length, verified_after_s: since(s.recovery_verified_at), revealed: !!s.revealed, outcome: s.outcome ?? null};
}

function timeline(id) {
  const s = sessions.get(id);
  if (!s) throw new HttpError(404, 'Investigation not found or summary retention expired');
  const points = evidence.filter(e => e.session_id === id && (e.kind === 'liveness' || e.kind === 'business')).sort((a, b) => a.at - b.at).slice(-1500)
    .map(e => ({at: e.at, node: e.node, kind: e.kind, ok: e.body.ok, slow: !!e.body.slow, ms: e.body.duration_ms ?? null, origin: e.body.origin}));
  const events = [{at: s.started_at, label: 'Investigation started'}];
  if (s.detected_at) events.push({at: s.detected_at, label: 'Failures detected'});
  for (const h of s.help) events.push({at: h.at, label: 'Help: ' + h.level});
  for (const a of s.recovery_attempts) events.push({at: a.at, label: `${a.verb || 'start'} ${a.node}: ${a.status}`});
  if (s.recovery_verified_at) events.push({at: s.recovery_verified_at, label: 'Recovery verified'});
  if (s.reset) events.push({at: s.reset.at, label: s.reset.automatic ? 'Automatic reset' : 'Reset requested'});
  if (s.ended_at) events.push({at: s.ended_at, label: 'Baseline verified, ended'});
  return {session_id: id, slow_ms: SLOW_MS, points, events: events.sort((a, b) => a.at - b.at)};
}

// ---- Routing, shaped like the FastAPI handlers ----
const nodeOk = value => NODES.includes(value);
const kindOk = value => ['liveness', 'readiness', 'business', 'logs', 'service', 'resources', 'disk', 'config', 'trace'].includes(value);
const invalid = () => { throw new HttpError(422, [{msg: 'Invalid request'}]); };

const routes = {
  'GET state': () => state(),
  'POST sessions': d => {
    if (!['practice', 'challenge'].includes(d.mode) || !nodeOk(d.target ?? 'catalog-b') || !((d.kind ?? 'service-unavailable') in FAULTS)) invalid();
    return start({target: 'catalog-b', kind: 'service-unavailable', ...d});
  },
  'POST check': d => (nodeOk(d.node) && kindOk(d.kind) ? check(d.node, d.kind) : invalid()),
  'POST recover': d => {
    const s = active(d.session_id);
    if (s.status === 'resetting') throw new HttpError(409, 'Reset is in progress. Wait for verification or retry reset.');
    const verb = d.verb || 'start';
    if (!['start', 'calm', 'clear', 'restore'].includes(verb)) invalid();
    const result = mutate(d.key, d.node || 'catalog-b', verb);
    if (!s.recovery_attempts.some(a => a.key === d.key)) {
      s.recovery_attempts.push({key: d.key, at: now(), ...result});
      s.status = 'verifying';
      s.verification = 'Waiting for fresh measured recovery; an accepted action is not proof';
      lab.successes = 0;
    }
    return result;
  },
  'POST reset': d => { active(d.session_id); return resetLocked(d.key); },
  'POST extend': d => {
    const s = active(d.session_id);
    if (['resetting', 'recovered'].includes(s.status)) throw new HttpError(409, 'The timer no longer applies');
    if (now() > s.deadline + 60) throw new HttpError(409, 'Continuation grace period ended; reset is required');
    s.extensions = s.extensions || [];
    if (!s.extensions.includes(d.key)) { s.deadline = Math.max(now(), s.deadline) + 900; s.extensions.push(d.key); }
    return publicSession();
  },
  'POST reveal': d => { const s = active(d.session_id); s.revealed = true; s.revealed_at = now(); return publicSession(); },
  'POST note': d => {
    const s = active(d.session_id), ids = d.evidence_ids || [];
    const known = new Set(evidenceByIds(s.id, ids).map(e => e.id));
    if (!ids.every(id => known.has(id))) throw new HttpError(422, 'Evidence must belong to this investigation and still be retained');
    s.notes.push({at: now(), explanation: String(d.explanation || ''), uncertainty: String(d.uncertainty || ''), evidence_ids: ids});
    return {saved: true};
  },
  'POST help': d => {
    const s = active(d.session_id);
    if (!['evidence', 'hint', 'action'].includes(d.level)) invalid();
    const guidance = explain(d.level);
    s.help.push({at: now(), level: d.level, evidence_ids: guidance.evidence_ids, message: guidance.message});
    return {...guidance, evidence: evidenceByIds(s.id, guidance.evidence_ids)};
  },
  'POST maintenance': () => ({evidence_removed: 0, summaries_removed: 0, batch_full: false, checkpoint_busy: false, storage: storage()}),
};

function investigation(path, query) {
  const [, id, sub, evidenceId] = path.split('/');
  const detail = sessionDetail(id);
  if (!sub) return detail;
  if (sub === 'timeline') return timeline(id);
  if (evidenceId) {
    const item = evidenceByIds(id, [evidenceId])[0];
    if (!item) throw new HttpError(404, 'Observation expired or does not belong to this investigation');
    return item;
  }
  const limit = Math.min(100, Math.max(1, Number(query.get('limit')) || 50));
  let items = sessionEvidence(id, Infinity);
  const before = query.get('before');
  if (before) {
    const index = items.findIndex(e => e.id === before);
    if (index < 0) throw new HttpError(409, 'Evidence cursor expired or does not belong to this investigation; reload the first page');
    items = items.slice(index + 1);
  }
  return {items: items.slice(0, limit), next_cursor: items.length > limit ? items[limit - 1].id : null};
}

function exportInvestigation(sessionId) {
  const sid = sessionId || lab.session?.id;
  if (!sid) throw new HttpError(409, 'Select an investigation from history to export');
  const detail = sessionDetail(sid);
  const cited = evidenceByIds(sid, citations(detail.session));
  const citedIds = new Set(cited.map(e => e.id));
  const selected = [...cited, ...sessionEvidence(sid, 500).filter(e => !citedIds.has(e.id))];
  return {...detail, evidence: selected, omitted_evidence_count: detail.evidence_summary.count - selected.length,
    export_note: 'SIMULATED investigation generated by the AegisRack browser preview. No real services were measured. Cited observations plus up to 500 recent records. Ground truth withheld until reveal/end.'};
}

async function handle(url, init = {}) {
  const parsed = new URL(url, location.href);
  const path = parsed.pathname.slice(parsed.pathname.indexOf('/api/') + 5);
  const method = (init.method || 'GET').toUpperCase();
  const data = init.body ? JSON.parse(init.body) : {};
  // Short waits keep the UI's busy and polling states visible, as with the real controller.
  await sleep(method === 'GET' ? 15 : path === 'check' ? 120 + Math.random() * 200 : 350 + Math.random() * 450);
  let status = 200, body;
  try {
    const route = routes[method + ' ' + path];
    if (route) body = route(data);
    else if (method === 'GET' && path.startsWith('investigations/')) body = investigation(path, parsed.searchParams);
    else throw new HttpError(404, 'Not found');
  } catch (error) {
    if (!(error instanceof HttpError)) throw error;
    status = error.status;
    body = {detail: error.message === '[object Object]' ? [{msg: 'Invalid request'}] : error.message};
  }
  return new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
}

const realFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  const url = typeof input === 'string' ? input : input.url;
  return /(^|\/)api\//.test(new URL(url, location.href).pathname) ? handle(url, init) : realFetch(input, init);
};

// Export links point at /api/export; build the file in the tab instead of navigating away.
document.addEventListener('click', event => {
  const link = event.target.closest?.('a[href*="api/export"]');
  if (!link) return;
  event.preventDefault();
  try {
    const sid = new URL(link.href, location.href).searchParams.get('session_id');
    const blob = new Blob([JSON.stringify(exportInvestigation(sid), null, 2)], {type: 'application/json'});
    const anchor = Object.assign(document.createElement('a'), {href: URL.createObjectURL(blob), download: 'aegis-investigation-simulated.json'});
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(anchor.href), 1000);
  } catch (error) {
    window.toast?.(error.message, true);
  }
}, true);

// Every visit and every reload starts with a reminder that this is a simulation.
document.addEventListener('DOMContentLoaded', () => {
  const welcome = document.getElementById('sim-welcome');
  if (!welcome) return;
  document.getElementById('sim-continue').addEventListener('click', () => {
    welcome.close();
    if (window.AegisTour && !window.AegisTour.seen()) window.AegisTour.start();
  });
  welcome.showModal();
});

// Preview-only control: skip ahead so visitors can see the expiry prompt and automatic reset.
document.addEventListener('DOMContentLoaded', () => {
  const skip = document.getElementById('sim-skip');
  if (!skip) return;
  const sync = () => {
    const s = lab.session;
    skip.disabled = !s || ['ended', 'recovered', 'resetting'].includes(s.status) || s.deadline - now() <= 45;
  };
  skip.addEventListener('click', () => {
    if (lab.session) skew += lab.session.deadline - now() - 40;
    sync();
  });
  setInterval(sync, 500);
});

setInterval(cycle, 2000);
cycle();
window.AegisSimulator = Object.freeze({lab, fleet, now});
})();
