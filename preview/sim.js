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

// ---- Simulated fleet: two services and the gateway's dependency log ----
const fleet = Object.fromEntries(NODES.map(id => [id, {container: hex(64), process: 'sim-' + crypto.randomUUID(), running: true, readyAt: 0}]));
const dependencyLog = [];
const answering = id => fleet[id].running && now() >= fleet[id].readyAt;
const catalogue = () => ({items: [{sku: 'LAB-001', name: 'Synthetic component', quantity: 12}], generation: fleet['catalog-b'].process});
const reply = (result, duration) => ({ok: true, http_status: 200, result, duration_ms: duration});

function probe(node, kind) {
  const n = fleet[node];
  if (kind === 'service') {
    return {ok: true, target: node, generation: n.container, running: n.running,
            status: n.running ? 'running' : 'exited', exit_code: 0, duration_ms: between(9, 30)};
  }
  if (!answering(node)) return {ok: false, error: 'ConnectError', detail: 'Check unavailable or failed', duration_ms: between(0.8, 4)};
  const role = node === 'gateway-a' ? 'gateway' : 'catalog';
  if (kind === 'liveness') return reply({live: true, role, generation: n.process}, between(3, 13));
  if (kind === 'logs') {
    const entries = role === 'gateway' ? dependencyLog.slice(-200) : [];
    return reply({entries, bounded_to: 200, generation: n.process, truncated: false, omitted_retained_entries: 0}, between(3, 9));
  }
  if (role === 'catalog') return reply(kind === 'readiness' ? {ok: true, generation: n.process} : catalogue(), between(5, 12));
  // Gateway readiness and business requests call the catalogue dependency.
  const correlation = crypto.randomUUID(), up = answering('catalog-b');
  const result = up
    ? {ok: true, dependency: 'catalog-b', correlation, catalog: catalogue(), generation: n.process, duration_ms: between(1.5, 6)}
    : {ok: false, dependency: 'catalog-b', correlation, error: 'ConnectError', generation: n.process, duration_ms: between(1, 5)};
  const {catalog: _, ...logged} = result;
  dependencyLog.push({at: now(), ...logged});
  if (dependencyLog.length > 200) dependencyLog.shift();
  return {ok: up, http_status: up ? 200 : 503, result, duration_ms: Math.round((result.duration_ms + between(2, 9)) * 10) / 10};
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
  if (kind === 'service') body.generation_scope = 'container';
  else if (body.result?.generation) Object.assign(body, {process_generation: body.result.generation, generation_scope: 'process'});
  const sid = lab.session && lab.session.status !== 'ended' ? lab.session.id : null;
  const item = record(sid, node, kind, body);
  lab.latest.set(node + '|' + kind, item);
  return item;
}

function cycle() {
  const results = NODES.flatMap(n => ['liveness', 'business'].map(k => check(n, k, 'collector')));
  const good = results.every(e => e.body.ok);
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
    if (s.revealed || s.status === 'ended') {
      result.debrief = 'The scenario requested stopping ' + s.fault_target + '. Control outcome: ' + (s.fault_action?.status || 'not recorded') + '. Compare its service state and failed probes with the other node: a live gateway can still fail business requests when its catalogue is unavailable. Starting the stopped service should restore the request path. The action record explains the exercise, but was not evidence available during investigation. Other evidence-supported investigation orders are valid.';
    }
  }
  return result;
}

function state() {
  const t = now();
  const nodes = NODES.map(id => {
    const live = lab.latest.get(id + '|liveness'), business = lab.latest.get(id + '|business');
    const fresh = live && business && [live, business].every(e => t - e.at >= 0 && t - e.at < 6);
    const health = !fresh ? 'unknown' : business.body.ok && live.body.ok ? 'healthy' : live.body.ok ? 'degraded' : 'unavailable';
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

// Docker-equivalent effects. Starting a service that is already running mirrors the
// real broker: Docker answers 304, so the controller records an unconfirmed outcome.
function mutate(key, node, verb) {
  const request = JSON.stringify({node, verb});
  const previous = actions.get(key);
  if (previous) {
    if (previous.request !== request || previous.session_id !== (lab.session?.id ?? null)) throw new HttpError(409, 'Idempotency key reused for a different operation');
    return previous.body;
  }
  const target = fleet[node];
  let body;
  if ((verb === 'start') === target.running) {
    body = {status: 'unknown', node, verb, detail: 'Inspect actual state before retrying; broker did not confirm completion'};
  } else {
    target.running = verb === 'start';
    if (target.running) Object.assign(target, {readyAt: now() + START_DELAY, process: 'sim-' + crypto.randomUUID()});
    body = {status: 'succeeded', node, verb, result: {target: node, generation: target.container, running: target.running, status: target.running ? 'running' : 'exited', exit_code: 0}};
  }
  actions.set(key, {request, session_id: lab.session?.id ?? null, at: now(), body});
  return body;
}

function save(s) { sessions.set(s.id, s); }

function start(data) {
  if (lab.session && lab.session.start_key === data.key) {
    if (lab.session.mode !== data.mode || (data.mode === 'practice' && lab.session.fault_target !== data.target)) throw new HttpError(409, 'Start key reused for a different investigation');
    return publicSession();
  }
  if (actions.has(data.key)) throw new HttpError(409, 'Start key already used; use a new request key');
  if (lab.session && lab.session.status !== 'ended') throw new HttpError(409, 'End/reset the current investigation before starting another');
  if (lab.baseline < 5 || !lab.lastCycle || now() - lab.lastCycle > 6) throw new HttpError(409, 'Five fresh healthy baseline cycles required');
  const target = data.mode === 'challenge' ? NODES[crypto.getRandomValues(new Uint8Array(1))[0] % 2] : data.target;
  if (!NODES.every(n => fleet[n].running)) {
    lab.baseline = 0;
    throw new HttpError(409, 'Fleet state changed since baseline; inspect and restore it before injection');
  }
  lab.session = {id: hex(32), mode: data.mode, status: 'observing', started_at: now(), deadline: now() + 900,
    fault_target: target, fault_kind: 'service-unavailable', revealed: false, help: [], notes: [], recovery_attempts: [],
    start_key: data.key, verification: 'Collecting measured effects', outcome: null};
  save(lab.session);
  lab.session.fault_action = mutate(data.key, target, 'stop');
  lab.baseline = lab.successes = lab.failures = 0;
  return publicSession();
}

function resetLocked(key, automatic = false) {
  const s = lab.session;
  if (!s) throw new HttpError(409, 'No investigation to reset');
  if (s.reset_key === key && (s.reset?.results || []).length === NODES.length) return publicSession();
  if (s.reset_key !== key) s.reset = {automatic, at: now(), results: []};
  s.reset_key = key;
  s.status = 'resetting';
  s.reset.results = NODES.map(node => mutate(key + '-' + node, node, 'start'));
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
  const t = now(), sid = lab.session.id;
  const fresh = new Map([...lab.latest].filter(([key, e]) => e.session_id === sid && t - e.at >= 0 && t - e.at <= (key.endsWith('|service') ? 30 : 6)));
  let ids = [], message = 'There is not enough fresh evidence yet. Start with a liveness check, then compare it with a business request.';
  let suggested = {node: 'gateway-a', kind: 'liveness'};
  const live = fresh.get('gateway-a|liveness'), business = fresh.get('gateway-a|business');
  if (live && business) {
    ids = [live.id, business.id];
    if (live.body.ok && !business.body.ok) {
      message = 'The gateway responds to liveness checks but its business request fails. Its own process is reachable; inspect the catalogue dependency before concluding the gateway is down.';
      suggested = {node: 'catalog-b', kind: 'service'};
    } else if (!live.body.ok) {
      message = 'The gateway liveness check failed. This does not by itself distinguish a stopped process from a connectivity problem. Inspect its container state and compare the catalogue independently.';
      suggested = {node: 'gateway-a', kind: 'service'};
    } else if (business.body.ok) {
      message = 'Recent gateway liveness and business checks succeeded. These observations do not establish an active failure; compare the incident timeline before changing anything.';
      suggested = {node: 'catalog-b', kind: 'liveness'};
    }
  }
  if (level === 'action') {
    const supported = [], contradicted = [];
    for (const [key, item] of fresh) {
      if (!key.endsWith('|service') || !item.body.ok || item.body.running !== false) continue;
      const newer = [...fresh].filter(([k, e]) => e.node === item.node && /\|(liveness|business)$/.test(k) && e.at > item.at && e.body.ok).map(([, e]) => e);
      if (newer.length) contradicted.push([item, newer.reduce((a, b) => (b.at > a.at ? b : a))]);
      else supported.push(item);
    }
    if (supported.length) {
      const item = supported.reduce((a, b) => (b.at > a.at ? b : a));
      ids = [item.id];
      message = `The recent service-state observation reports ${item.node} stopped. You can try starting that selected service, then verify fresh business requests. This does not establish why it stopped.`;
      suggested = {node: item.node, kind: 'service'};
    } else if (contradicted.length) {
      const [old, newer] = contradicted.reduce((a, b) => (b[1].at > a[1].at ? b : a));
      ids = [old.id, newer.id];
      message = `The stopped-state observation for ${old.node} is older than a successful check from that node. Its state may have changed. Inspect its container state again before choosing a recovery action.`;
      suggested = {node: old.node, kind: 'service'};
    } else {
      message += ' No fresh stopped-container observation supports a targeted start yet. Gather the suggested check first.';
    }
  } else if (level === 'hint') {
    message += ' Compare process state with request behaviour; one failed check is not a complete diagnosis.';
  }
  return {level, message, evidence_ids: ids, suggested_check: suggested, source: 'deterministic_observation_rules', observed_at: t};
}

function citations(s) {
  const ids = [...s.notes, ...s.help].flatMap(n => n.evidence_ids || []);
  return [...new Set([...ids, ...(s.recovery_evidence_ids || []), ...(s.reset_evidence_ids || [])])];
}
function sessionDetail(id) {
  const s = sessions.get(id);
  if (!s) throw new HttpError(404, 'Investigation not found or summary retention expired');
  const present = new Set(evidenceByIds(id, citations(s)).map(e => e.id));
  return {session: publicSession(s), evidence_summary: evidenceSummary(id), missing_citations: citations(s).filter(e => !present.has(e))};
}

// ---- Routing, shaped like the FastAPI handlers ----
const nodeOk = value => NODES.includes(value);
const kindOk = value => ['liveness', 'readiness', 'business', 'logs', 'service'].includes(value);
const invalid = () => { throw new HttpError(422, [{msg: 'Invalid request'}]); };

const routes = {
  'GET state': () => state(),
  'POST sessions': d => {
    if (!['practice', 'challenge'].includes(d.mode) || !nodeOk(d.target ?? 'catalog-b')) invalid();
    return start({target: 'catalog-b', ...d});
  },
  'POST check': d => (nodeOk(d.node) && kindOk(d.kind) ? check(d.node, d.kind) : invalid()),
  'POST recover': d => {
    const s = active(d.session_id);
    if (s.status === 'resetting') throw new HttpError(409, 'Reset is in progress. Wait for verification or retry reset.');
    const result = mutate(d.key, d.node || 'catalog-b', 'start');
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
