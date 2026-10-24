'use strict';
// Browser-only stand-in for the AegisRack controller, broker and fleet.
// It mirrors the rules in aegis/controller.py and aegis/guidance.py so the
// unmodified web UI can run on a static host. Every measurement it returns is
// generated in this tab; nothing here touches a container or a network service.
(() => {
const NODES = ['gateway-a', 'catalog-b', 'gateway-c'];
const GATEWAYS = ['gateway-a', 'gateway-c'];
const isGateway = node => node.startsWith('gateway');
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

// ---- Simulated fleet: three services, their scratch disks, runaway jobs and logs ----
// Latencies under a runaway job follow the real lab's measurements (catalogue median ~400-600 ms).
const SLOW_MS = 300;
const DISK = 8 * 1024 * 1024;
const fleet = Object.fromEntries(NODES.map(id => [id, {container: hex(64), process: 'sim-' + crypto.randomUUID(), running: true, readyAt: 0,
  busy: false, full: false, drifted: false, delayed: false, lossy: false, squeezed: false, unresolved: false, dnsFailsAt: null, partitioned: false, skewed: false, periods: 4000, throttled: 0, logs: [], traffic: [],
  version: 'v1.4.2', history: [{version: 'v1.4.2', at: now(), by: 'deployment', note: 'stable build'}], leakAt: null, oomKilled: false, exitCode: 0, startedAt: now(), finishedAt: null}]));
const answering = id => { tick(id); return fleet[id].running && now() >= fleet[id].readyAt; };
const catalogue = () => ({items: [{sku: 'LAB-001', name: 'Synthetic component', quantity: 12}], generation: fleet['catalog-b'].process});
const reply = (result, duration) => ({ok: true, http_status: 200, result, duration_ms: duration});
// A service reports times on its own wall clock, which the clock-skew fault steps CLOCK_STEP seconds ahead.
const CLOCK_STEP = 300, BAD_TIME_SOURCE = '10.20.0.7 (falseticker)';
const wall = node => now() + (fleet[node].skewed ? CLOCK_STEP : 0);
const expired = () => ({error: 'RequestExpired', detail: `timestamp +${CLOCK_STEP}.0 s from the catalogue clock; allowed ±30 s`});
const log = (node, entry) => { const logs = fleet[node].logs; logs.push({at: wall(node), generation: fleet[node].process, ...entry}); if (logs.length > 200) logs.shift(); };
// Network delay: everything a delayed node sends waits once. Calibrated on the real lab (68 rounds, ~4,400 checks):
// a delayed catalogue's checks ~606-608 ms, callers ~612 ms, a delayed gateway's requests ~1,210 ms (outbound call
// and reply), no failures, and fewer than 0.5% of checks paying an extra delay.
const DELAY = 600;
const lag = node => (fleet[node].delayed ? DELAY + (Math.random() < 0.004 ? DELAY : 0) + between(0.5, 2.5) : 0);
// Packet loss: TCP resends a lost packet after at least 200 ms and backs off on repeats. Calibrated on the real lab at
// 50% loss (22 October 2026, about 280 checks per path). Each row: cumulative share, then the added delay range or
// the error. A gateway's call to a lossy catalogue (or from a lossy gateway) gives up at 1 second:
const LOSS = 50;
const CALL = [[0.39, 0, 0], [0.655, 200, 214], [0.685, 400, 480], [0.755, 600, 880], [0.9, 'ConnectTimeout'], [0.975, 'ReadTimeout'], [1, 'ReadError']];
// A lossy service's replies to the controller, which waits up to 3 seconds:
const REPLY = [[0.45, 0, 0], [0.62, 200, 214], [0.705, 400, 480], [0.77, 600, 880], [0.845, 1000, 1450], [0.885, 1600, 2600], [1, 'ReadTimeout']];
function draw(table) {
  const r = Math.random(), [, low, high] = table.find(([share]) => r < share);
  if (typeof low === 'string') return {extra: low === 'ReadError' ? between(150, 600) : table === CALL ? 1000 : 3000, error: low};
  return {extra: between(low, high), error: null};
}
const hop = node => (fleet[node].lossy ? draw(REPLY) : {extra: 0, error: null});
// One gateway call to the catalogue crosses the catalogue's egress or the gateway's own; only one can be lossy.
const call = node => (fleet['catalog-b'].lossy || fleet[node].lossy ? draw(CALL) : {extra: 0, error: null});
// Business traffic by caller, as aegis/fleet.py counts it, and the catalogue's per-caller quotas.
const QUOTA_DEPLOYED = {'gateway-a': 100, 'gateway-c': 100};
const quotaOf = (node, client) => (node === 'catalog-b' && client in QUOTA_DEPLOYED ? (fleet[node].squeezed ? 0 : QUOTA_DEPLOYED[client]) : null);
const count = (node, client, status) => { const t = fleet[node].traffic; t.push({at: now(), client, status}); if (t.length > 4000) t.shift(); };
const quotaRefusal = client => ({ok: false, error: 'QuotaExceeded', client, limit_rps: 0, detail: 'per-client request quota exhausted', generation: fleet['catalog-b'].process});
// Releases, as aegis/fleet.py models them. Measured on the real lab (22 October 2026): the bad release failed 47% of
// business requests; the leaky one grew about 12 MiB a second from about 46 MiB until the kernel killed the process at
// the 192 MiB limit (OOMKilled, exit 137), roughly 12-14 seconds after it was deployed.
const STABLE = 'v1.4.2', BAD_RELEASE = 'v1.5.0', LEAKY_RELEASE = 'v1.5.1', MEMORY_LIMIT = 192, LEAK_MIB_S = 12.1;
const RELEASE_NOTES = {[STABLE]: 'stable build', [BAD_RELEASE]: 'faster catalogue lookups', [LEAKY_RELEASE]: 'warm the item cache in the background'};
const ROLLOUTS = {'bad-release': ['ship', BAD_RELEASE], 'memory-leak': ['leak', LEAKY_RELEASE]};
const releaseFails = node => fleet[node].version === BAD_RELEASE && Math.random() < 0.47;
const typeError = node => ({ok: false, error: 'TypeError', detail: "unsupported operand type(s) for *: 'NoneType' and 'float'", path: 'price conversion', version: BAD_RELEASE, generation: fleet[node].process});
const iso = t => new Date(t * 1000).toISOString();
function memoryAt(node, t) {
  const n = fleet[node], idle = 45 + Math.sin(t / 7) * 0.8;
  return Math.round((n.leakAt != null && t >= n.leakAt ? idle + LEAK_MIB_S * (t - n.leakAt) : idle) * 10) / 10;
}
// A leaking process that reaches the container's memory limit is killed by the kernel.
function tick(node) {
  const n = fleet[node];
  if (!n.running || n.leakAt == null) return;
  const killedAt = n.leakAt + (MEMORY_LIMIT - memoryAt(node, n.leakAt)) / LEAK_MIB_S;
  if (now() >= killedAt) Object.assign(n, {running: false, oomKilled: true, exitCode: 137, finishedAt: killedAt, leakAt: null});
}
function deploy(node, version, by) {
  const n = fleet[node];
  if (n.version === version) return false;
  log(node, {ok: true, event: 'release deployed', version, previous: n.version, by});
  n.version = version;
  n.history.push({version, at: now(), by, note: RELEASE_NOTES[version]});
  n.leakAt = version === LEAKY_RELEASE ? now() : null;
  return true;
}
function memoryReport(node) {
  const n = fleet[node], t = now(), boot = n.readyAt || n.startedAt;
  const samples = [];
  for (let at = Math.max(boot, t - 60); at <= t; at += 2) samples.push({age_s: Math.round((t - at) * 10) / 10, mib: memoryAt(node, at)});
  // The real lab reports about 12 MiB a second within seconds of a leak starting, so measure from its start.
  const since = n.leakAt != null ? t - n.leakAt : 20;
  const recent = samples.filter(s => s.age_s <= Math.min(20, since)), span = recent.length > 1 ? recent[0].age_s - recent[recent.length - 1].age_s : 0;
  const current = memoryAt(node, t);
  return {current_mib: current, limit_mib: MEMORY_LIMIT, used_pct: Math.round(current / MEMORY_LIMIT * 1000) / 10,
    growth_mib_s: span >= 4 ? Math.round((recent[recent.length - 1].mib - recent[0].mib) / span * 100) / 100 : null,
    samples: samples.reverse().slice(0, 30).reverse(), oom_kills: 0, uptime_s: Math.round((t - boot) * 10) / 10, version: n.version, generation: n.process};
}
// The report exporter's flood, as the reports service runs it. Measured on the real lab (23 October 2026, 256
// connections): about 480 requests a second held the catalogue at its CPU limit, its own checks took about 575 ms and
// the gateways' about 500 ms, with no failures. Rate-limited, it is refused with 429 and backs off with jitter.
const flood = {start: null, limitedAt: null, stoppedAt: null};
const FLOOD_RPS = 480, LIMITED_RPS = 11;
const flooded = () => flood.start != null && flood.stoppedAt == null && flood.limitedAt == null;
const floodMs = () => between(500, 600);
function exporterTraffic(t) {
  if (flood.start == null) return null;
  const from = t - 60, overlap = (a, b) => Math.max(0, Math.min(b, t) - Math.max(a, from));
  const end = flood.stoppedAt ?? t, limited = flood.limitedAt ?? end;
  const heavy = overlap(flood.start, limited), slow = overlap(limited, end);
  const requests = Math.round(heavy * FLOOD_RPS + slow * LIMITED_RPS), rejected = Math.round(slow * (LIMITED_RPS - 2));
  const recent = flooded() ? FLOOD_RPS : flood.limitedAt != null && flood.stoppedAt == null ? LIMITED_RPS : 0;
  return requests ? {client: 'report-exporter', requests, rejected, errors: 0, rps: Math.round(requests / 60 * 100) / 100, recent_rps: recent} : null;
}
// The retry storm, as the reports service and gateways run it. Measured on the real lab (22 October 2026): 60 app users
// a second through one gateway, retrying failures at once up to three times. Six seconds in, the catalogue's 8-second
// compaction made requests time out; the retries then held the gateway at its CPU limit with most requests failing,
// about 220 attempts a second, 70% of them retries, until load shedding brought it back within about 10 seconds.
const storm = {target: null, start: null, shedAt: null, stoppedAt: null, shedding: new Set()};
const HICCUP_AFTER = 6, HICCUP_S = 8, USER_RPS = 60, STORM_RPS = 220;
// In the compound incident the power cap is the trigger, not a compaction: the storm starts with the users.
const stormFrom = () => storm.start + (storm.capTriggered ? 0 : HICCUP_AFTER);
const hiccup = () => storm.start != null && !storm.capTriggered && storm.stoppedAt == null && now() >= storm.start + HICCUP_AFTER && now() < storm.start + HICCUP_AFTER + HICCUP_S && !storm.hiccupStopped;
const storming = node => storm.target === node && storm.stoppedAt == null && storm.shedAt == null && now() >= stormFrom();
const settling = node => storm.target === node && storm.shedAt != null && now() < storm.shedAt + 4;  // shedding takes a few seconds to bite
function usersTraffic(node, t) {
  if (storm.target !== node || storm.start == null) return null;
  const from = t - 60, end = storm.stoppedAt ?? t, overlap = (a, b) => Math.max(0, Math.min(b, end) - Math.max(a, from));
  const heavy = overlap(stormFrom(), storm.shedAt ?? end), calm = overlap(storm.start, end) - heavy;
  const requests = Math.round(heavy * STORM_RPS + calm * USER_RPS), retries = Math.round(heavy * STORM_RPS * 0.7);
  const recent = storm.stoppedAt != null ? 0 : storming(node) ? STORM_RPS : USER_RPS;
  return requests ? {client: 'mobile-app', requests, rejected: 0, errors: Math.round(heavy * STORM_RPS * 0.95), retries, rps: Math.round(requests / 60 * 100) / 100,
    recent_rps: recent, recent_retries: storming(node) ? Math.round(STORM_RPS * 10 * 0.7) : 0} : null;
}
// The facility layer, simulated in the real lab too: racks, PDUs and breakers. A tripped breaker stops every service
// it feeds, and none can start until it is reset.
const RACKS = {'PDU-A': {rack: 'R1', nodes: ['catalog-b', 'gateway-a']}, 'PDU-B': {rack: 'R2', nodes: ['gateway-c']}};
const PDU_OF = Object.fromEntries(Object.entries(RACKS).flatMap(([pdu, r]) => r.nodes.map(n => [n, pdu])));
const facility = {};
// A brownout: feed A to a PDU sags below what a power supply tolerates, so the server drops out, reboots and drops out
// again, as aegis/controller.py drives it: stopped after 5 health cycles up, started again after 3 down.
const BROWNOUT_UP = 5, BROWNOUT_DOWN = 3, NOMINAL_V = 230, SAG_V = 186, PSU_MIN_V = 200;
const sagging = body => body?.feeds?.[body.active_feed || 'A']?.status === 'sagging';
// The thermal fault: a failed cooling unit, a power cap of 0.05 CPU on every container and steady daytime users.
// Measured on the real lab (23 October 2026, 32 users a second per gateway): the gateways ran out first, their liveness
// slowed to a median of about 160 ms (p90 200-500 ms) and about 85% of business requests failed, while the catalogue's
// simple answers stayed fast.
const COOLING_RECOVERY = 75, HOT_INLET = 38.4, DAYTIME_RPS = 32;
const room = {failedAt: null, capped: false, liftedAt: null};
const roomHot = () => room.failedAt != null && now() < room.failedAt + COOLING_RECOVERY;
const capped = () => room.capped;
const cappedGateway = node => capped() && isGateway(node);
const pick3 = () => { const r = Math.random(); return r < 0.25 ? 0 : r < 0.62 ? 1 : 2; };  // measured mix: timeouts, busy, dropped
const breakerOpen = node => facility[PDU_OF[node]]?.breaker === 'open';
function facilityReport(node) {
  const pdu = PDU_OF[node], state = facility[pdu] || {}, powered = state.breaker !== 'open';
  const drawn = RACKS[pdu].nodes.filter(n => powered && fleet[n].running).length;
  const events = [...(state.at ? [{at: state.at, event: 'breaker tripped', pdu, cause: 'overcurrent on circuit 3'}] : []), ...(state.closedAt ? [{at: state.closedAt, event: 'breaker reset', pdu}] : [])];
  const hot = roomHot();
  if (room.failedAt != null) {
    events.push({at: room.failedAt, event: 'cooling unit CRAC-2 failed'}, {at: room.failedAt + 1, event: 'power cap applied to racks R1 and R2', cpu_limit: 0.05});
    if (!hot) events.push({at: room.failedAt + COOLING_RECOVERY, event: 'cooling unit CRAC-2 restored'});
  }
  if (room.liftedAt != null) events.push({at: room.liftedAt, event: 'power cap lifted'});
  const sag = state.sag || {};
  if (sag.at != null) {
    events.push({at: sag.at, event: `utility feed A sagging to ${SAG_V} V`, pdu});
    for (const at of (sag.drops || []).slice(-6)) events.push({at, event: 'power supply dropout', service: sag.node, input_v: SAG_V - 7, min_v: PSU_MIN_V});
  }
  if (sag.transferredAt != null) events.push({at: sag.transferredAt, event: 'rack moved to redundant feed B', pdu});
  const feeds = {A: {voltage_v: sag.active ? SAG_V : NOMINAL_V, status: sag.active ? 'sagging' : 'normal'}, B: {voltage_v: NOMINAL_V + 1, status: 'normal'}};
  return {ok: true, simulated: true, rack: RACKS[pdu].rack, pdu, breaker: powered ? 'closed' : 'open', fed_services: [...RACKS[pdu].nodes],
    load_kw: Math.round((0.11 * drawn + (powered ? 0.04 : 0)) * 100) / 100, inlet_c: hot ? HOT_INLET : pdu === 'PDU-A' ? 24.8 : 25.3,
    cooling: {crac_2: hot ? 'failed' : 'running'}, power_cap: {active: room.capped, cpu_limit: room.capped ? 0.05 : null},
    ups: {on_battery: false, charge_pct: 100}, feeds, active_feed: state.feed || 'A', psu_min_v: PSU_MIN_V, events: events.sort((a, b) => a.at - b.at), duration_ms: between(1, 3)};
}
const busyMs = () => Math.round(Math.min(1600, 110 + Math.random() ** 1.6 * 1100) * 10) / 10;
const diskFull = node => ({ok: false, error: 'OSError', errno: 'ENOSPC', detail: 'No space left on device', path: 'receipt write', generation: fleet[node].process});
// Runtime settings as aegis/fleet.py reports them; drift overrides one, as a bad hot reload would.
const UPSTREAM = 'http://catalog-b:8000';
const DEPLOYED = {'gateway-a': {upstream: UPSTREAM, upstream_timeout_s: 1.0}, 'gateway-c': {upstream: UPSTREAM, upstream_timeout_s: 1.0}, 'catalog-b': {catalogue_source: 'builtin:lab-v1', receipt_ring: 40}};
const DRIFT = {'gateway-a': ['upstream', 'http://catalog-b:8080'], 'gateway-c': ['upstream', 'http://catalog-b:8080'], 'catalog-b': ['catalogue_source', 'file:/tmp/catalogue-v2.json']};
const settings = node => Object.entries(DEPLOYED[node]).map(([key, deployed]) => {
  const value = fleet[node].drifted && DRIFT[node][0] === key ? DRIFT[node][1] : deployed;
  return {key, value, deployed, drifted: value !== deployed};
});
// Name resolution: a gateway looks up the catalogue with real DNS queries and caches each answer for 5 seconds, so a
// repointed resolver only bites once the cached answer expires.
const DNS_TTL = 5, BROKEN_NAMESERVER = '127.0.0.53', NAMESERVER = '127.0.0.11', CATALOGUE_ADDRESS = '172.18.0.4';
const dnsBroken = node => isGateway(node) && fleet[node].unresolved && now() >= fleet[node].dnsFailsAt;
const lookupError = () => `catalog-b: connection refused by nameserver ${BROKEN_NAMESERVER}`;
// Docker's resolver drops the name of a stopped container, so once the cached answer expires a stopped catalogue
// fails even to resolve (measured on the real lab: SERVFAIL from 127.0.0.11).
const nameGone = () => !fleet['catalog-b'].running && now() - (fleet['catalog-b'].finishedAt || 0) > DNS_TTL / 2;
const goneError = () => `catalog-b: SERVFAIL from nameserver ${NAMESERVER}`;
const sourceMissing = node => ({ok: false, error: 'FileNotFoundError', detail: 'catalogue source unavailable', source: DRIFT[node][1], generation: fleet[node].process});

// One traced business request, shaped like /diagnostics/trace.
function trace(node) {
  const n = fleet[node], spans = [];
  let clock = between(0.1, 0.4);
  const span = (name, ms, ok, detail = {}) => { spans.push({name, start_ms: Math.round(clock * 10) / 10, ms, ok, ...detail}); clock += ms; return ok; };
  const own = (low, high) => n.busy ? Math.round(busyMs() * 0.4 * 10) / 10 : between(low, high);
  let ok;
  if (isGateway(node)) {
    const [address] = (n.drifted ? DRIFT[node][1] : UPSTREAM).split('//').slice(-1), name = `upstream ${address}/catalog`, cat = fleet['catalog-b'];
    const net = call(node);
    const failed = dnsBroken(node) ? lookupError() : nameGone() ? goneError() : null;
    const lookup = span('dns lookup catalog-b', between(0.2, 0.9), !failed, failed ? {error: 'NameResolutionError', detail: failed} : {});
    if (!lookup) ok = false;
    else if (n.partitioned) ok = span(name, 1000, false, {error: 'ConnectTimeout'});  // the handshake is never answered
    else if (n.drifted || !answering('catalog-b')) ok = span(name, between(0.8, 3), false, {error: 'ConnectError'});
    else if (net.error) ok = span(name, net.extra, false, {error: net.error});
    else if (n.skewed) { count('catalog-b', node, 401); log('catalog-b', {ok: false, client: node, skew_s: CLOCK_STEP, ...expired()}); ok = span(name, between(1.5, 4), false, {status: 401}); }
    else if (cat.squeezed) { count('catalog-b', node, 429); ok = span(name, between(1.5, 4), false, {status: 429}); }
    else if (releaseFails('catalog-b')) { count('catalog-b', node, 500); log('catalog-b', typeError('catalog-b')); ok = span(name, between(2, 6), false, {status: 500}); }
    else if (cat.drifted) ok = span(name, between(2, 6), false, {status: 500});
    else if (cat.full) ok = span(name, between(2, 6), false, {status: 507});
    else {
      // httpx applies the 1-second timeout to each wait, so only a slow catalogue handler can exceed it, not the delay.
      const wait = cat.busy || hiccup() ? busyMs() : flooded() ? floodMs() : between(1.5, 6), ms = Math.round((wait + lag('catalog-b') + lag(node) + net.extra) * 10) / 10;
      count('catalog-b', node, 200);  // the catalogue still answers; the gateway may already have given up
      ok = wait > 1000 ? span(name, 1000, false, {error: 'ReadTimeout'}) : span(name, ms, true, {status: 200});
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
  tick(node);
  const container = {generation: n.container, running: n.running, status: n.running ? 'running' : 'exited', exit_code: n.exitCode, oom_killed: n.oomKilled,
    started_at: iso(n.startedAt), finished_at: n.finishedAt ? iso(n.finishedAt) : '0001-01-01T00:00:00Z'};
  if (kind === 'service') return {ok: true, target: node, ...container, duration_ms: between(9, 30)};
  if (kind === 'facility') return facilityReport(node);
  if (kind === 'memory') {
    if (!n.running) return {ok: true, ...container, duration_ms: between(9, 30)};
    if (!answering(node)) return {ok: true, ...container, service_error: 'ConnectError', duration_ms: between(9, 30)};
    return {ok: true, ...container, http_status: 200, result: memoryReport(node), duration_ms: between(12, 40)};
  }
  if (kind === 'resources') {
    const base = {ok: true, target: node, ...container, cpu_limit: capped() ? 0.05 : 0.25};
    if (!n.running) return {...base, duration_ms: between(9, 30)};
    // A process that has just started uses its whole limit while it boots, as on the real lab.
    const booting = now() - n.startedAt < 4;
    const limit = capped() ? 0.05 : 0.25;
    const cores = Math.round((cappedGateway(node) ? limit * (0.96 + Math.random() * 0.04) : capped() ? limit * (0.25 + Math.random() * 0.15) : n.busy || booting || (node === 'catalog-b' && (flooded() || hiccup())) || storming(node) ? 0.24 + Math.random() * 0.01 : 0.004 + Math.random() * 0.016) * 1000) / 1000;
    n.periods += 10; if (n.busy) n.throttled += 9;
    return {...base, cpu_cores: cores, cpu_pct_of_limit: Math.round(cores / limit * 1000) / 10, periods: n.periods, throttled_periods: n.throttled,
            throttled_ms: Math.round(n.throttled * 75 * 10) / 10, memory_mib: memoryAt(node, now()), memory_limit_mib: MEMORY_LIMIT, duration_ms: between(1000, 1250)};
  }
  if (!answering(node)) return {ok: false, error: kind === 'network' ? 'ConnectionRefusedError' : 'ConnectError', detail: 'Check unavailable or failed', duration_ms: between(0.8, 4)};
  // A lossy service's reply can be lost so often that the controller's 3-second wait runs out.
  const replyHop = kind === 'network' ? {extra: 0, error: null} : hop(node);
  if (replyHop.error) return {ok: false, error: replyHop.error, detail: 'Check unavailable or failed', duration_ms: replyHop.extra};
  if (kind === 'network') {
    // A lost handshake reply is resent after the kernel's initial 1-second retransmission timeout.
    // Measured at 50% loss: about 45% of attempts are quick, 35% wait one resent handshake (~1 s), 13% two (~2 s), the rest give up at 3 s.
    const connect = [0, 1, 2].map(() => { const r = Math.random();
      return n.delayed ? DELAY + between(0.3, 1.5) : !n.lossy || r < 0.45 ? between(0.2, 0.9) : r < 0.8 ? between(1020, 1070) : r < 0.93 ? between(2040, 2070) : 3000; });
    const timeouts = connect.filter(ms => ms === 3000).length;
    const queue = n.partitioned ? {kind: 'prio', dev: 'eth0', root: true, delay_ms: null, loss_pct: null, line: 'qdisc prio 1: dev eth0 root refcnt 2 bands 3 priomap 1 2 2 2 1 2 0 0 1 1 1 1 1 1 1 1'}
      : n.delayed ? {kind: 'netem', dev: 'eth0', root: true, delay_ms: DELAY, loss_pct: null, line: `qdisc netem 8001: dev eth0 root refcnt 2 limit 1000 delay ${DELAY}ms`}
      : n.lossy ? {kind: 'netem', dev: 'eth0', root: true, delay_ms: null, loss_pct: LOSS, line: `qdisc netem 8001: dev eth0 root refcnt 2 limit 1000 loss ${LOSS}% seed 12772983829579208008`}
      : {kind: 'noqueue', dev: 'eth0', root: true, delay_ms: null, loss_pct: null, line: 'qdisc noqueue 0: dev eth0 root refcnt 2'};
    // Retransmissions show at both ends: the lossy service resends a third to a half of its segments, and its peers
    // resend a few when their acknowledgements are lost.
    const peerLossy = isGateway(node) ? fleet['catalog-b'].lossy : NODES.some(g => isGateway(g) && fleet[g].lossy);
    // A partitioned gateway resends every unanswered connection setup to the catalogue.
    const sent = Math.round(between(20, 110)), resent = n.lossy ? between(31, 60) : n.partitioned ? between(40, 70) : peerLossy ? (Math.random() < 0.4 ? 0 : between(2, 19)) : 0;
    const tcp = {available: true, since: 'previous network check', interval_s: between(4, 30), out_segs: sent, retrans_segs: Math.round(sent * resent / 100), retrans_pct: Math.round(resent * 10) / 10};
    const clock = {now: Math.round(wall(node) * 1000) / 1000, source: n.skewed ? 'time daemon, peer ' + BAD_TIME_SOURCE : 'host kernel clock', last_step_s: n.skewed ? CLOCK_STEP : null,
      stepped_at: n.skewedAt ?? null, signing: {enabled: true, window_s: 30}};
    return {ok: true, http_status: 200, connect_ms: connect, connect_timeouts: timeouts, median_connect_ms: [...connect].sort((a, b) => a - b)[1],
      clock_offset_s: Math.round(((n.skewed ? CLOCK_STEP : 0) + (Math.random() - 0.5) * 0.004) * 100) / 100,
      result: {available: true, qdiscs: [queue], filters: n.partitioned ? [{kind: 'u32', dst: CATALOGUE_ADDRESS + '/32', action: 'drop'}] : [], tcp,
        ...(isGateway(node) ? {dns: dnsReport(node), upstream_connect: upstreamConnect(node)} : {}), clock, generation: n.process},
      duration_ms: Math.round(connect.reduce((a, b) => a + b, 0) * 10) / 10 + between(3, 9)};
  }
  const answer = answered(node, kind);
  // Everything the node sends back waits in its delay queue too, and a lossy node's replies are sometimes resent.
  if (n.delayed) answer.duration_ms = Math.round((answer.duration_ms + lag(node)) * 10) / 10;
  if (n.lossy) answer.duration_ms = Math.round((answer.duration_ms + replyHop.extra) * 10) / 10;
  return answer;
}

// A TCP connection from a gateway to the address its upstream name resolves to, as aegis/fleet.py makes it.
function upstreamConnect(node) {
  if (dnsBroken(node)) return {name: 'catalog-b', ok: false, error: 'NameResolutionError', detail: lookupError()};
  if (nameGone()) return {name: 'catalog-b', ok: false, error: 'NameResolutionError', detail: goneError()};
  const base = {name: 'catalog-b', address: CATALOGUE_ADDRESS, port: 8000};
  if (fleet[node].partitioned) return {...base, ok: false, error: 'TimeoutError', waited_ms: between(1000, 1003)};
  return answering('catalog-b') ? {...base, ok: true, connect_ms: between(0.2, 0.9)} : {...base, ok: false, error: 'ConnectionRefusedError', waited_ms: between(0.2, 0.6)};
}

function dnsReport(node) {
  const n = fleet[node], nameserver = n.unresolved ? BROKEN_NAMESERVER : NAMESERVER;
  const base = {name: 'catalog-b', nameserver, deployed_nameserver: NAMESERVER, changed: n.unresolved, pushed_by: n.unresolved ? 'node-settings-sync' : 'deployment'};
  if (n.unresolved) return {...base, ok: false, error: lookupError(), lookup_ms: between(0.2, 0.6), last_good_address: CATALOGUE_ADDRESS,
    ...(answering('catalog-b') ? {connect_last_good_ms: between(0.2, 0.9)} : {connect_last_good_error: 'ConnectionRefusedError'})};
  if (nameGone()) return {...base, ok: false, error: goneError(), lookup_ms: between(0.2, 0.6), last_good_address: CATALOGUE_ADDRESS, connect_last_good_error: 'TimeoutError'};
  return {...base, ok: true, address: CATALOGUE_ADDRESS, ttl_s: 600, lookup_ms: between(0.3, 1.2)};
}

function answered(node, kind) {
  const n = fleet[node];
  const role = isGateway(node) ? 'gateway' : 'catalog';
  const quick = (low, high) => cappedGateway(node) ? (Math.random() < 0.8 ? between(120, 220) : between(220, 520)) : n.busy || (node === 'catalog-b' && hiccup()) ? busyMs() : node === 'catalog-b' && flooded() ? floodMs() : storming(node) || settling(node) ? between(300, 900) : between(low, high);
  if (kind === 'liveness') return reply({live: true, role, generation: n.process, version: n.version}, quick(3, 13));
  if (kind === 'releases') return reply({running: n.version, note: RELEASE_NOTES[n.version], history: n.history.slice(-10), uptime_s: Math.round((now() - n.startedAt) * 10) / 10, generation: n.process}, quick(3, 9));
  if (kind === 'logs') return reply({entries: n.logs.slice(-200), bounded_to: 200, generation: n.process, truncated: false, omitted_retained_entries: 0}, quick(3, 9));
  if (kind === 'disk') {
    const free = n.full ? 0 : DISK - 40 * 4096;
    const largest = n.full ? [{path: 'aegis-fill.bin', bytes: DISK - 40 * 4096}, {path: 'receipts/00000412.json', bytes: 230}] : [{path: 'receipts/00000412.json', bytes: 230}];
    return reply({mount: '/tmp', total_bytes: DISK, free_bytes: free, used_pct: Math.round((DISK - free) / DISK * 1000) / 10, largest, file_count: 40 + (n.full ? 1 : 0), generation: n.process}, quick(3, 9));
  }
  if (kind === 'config') return reply({role, settings: settings(node), drifted: settings(node).filter(s => s.drifted).map(s => s.key), generation: n.process}, quick(3, 9));
  if (kind === 'trace') return trace(node);
  if (kind === 'traffic') {
    const t = now(), callers = {};
    for (const e of n.traffic) {
      if (t - e.at > 60) continue;
      const c = callers[e.client] ||= {client: e.client, requests: 0, rejected: 0, errors: 0, retries: 0};
      c.requests += 1; c.rejected += e.status === 429 || e.status === 401; c.errors += e.status >= 500;
    }
    const daytime = role !== 'catalog' && room.failedAt != null && room.daytimeStoppedAt == null ? {client: 'mobile-app', requests: Math.round(Math.min(60, t - room.failedAt) * DAYTIME_RPS), rejected: 0, errors: 0, retries: 0, rps: DAYTIME_RPS * Math.min(60, t - room.failedAt) / 60, recent_rps: DAYTIME_RPS} : null;
    const exporter = role === 'catalog' ? exporterTraffic(t) : usersTraffic(node, t) || daytime;
    const recentCount = client => n.traffic.filter(e => e.client === client && t - e.at <= 10).length;
    const clients = [...Object.values(callers).map(c => ({...c, rps: Math.round(c.requests / 60 * 100) / 100, recent_rps: Math.round(recentCount(c.client) / 10 * 100) / 100})),
      ...(exporter ? [exporter] : [])].sort((a, b) => b.requests - a.requests);
    const quotas = role === 'catalog' ? [...Object.entries(QUOTA_DEPLOYED).map(([client, deployed]) => ({client, limit_rps: quotaOf(node, client), deployed_rps: deployed, changed: quotaOf(node, client) !== deployed})),
      ...(flood.limitedAt != null ? [{client: 'report-exporter', limit_rps: 2, deployed_rps: null, changed: true}] : [])] : [];
    return reply({window_s: 60, clients, quotas, quota_source: role === 'catalog' ? {pushed_by: n.squeezed ? 'quota-manager' : 'deployment', at: n.quotaAt || t - 3600} : null,
      signing: {enabled: true, signed_callers: role === 'catalog' ? ['gateway-a', 'gateway-c'] : [], window_s: 30}, generation: n.process}, quick(3, 9));
  }
  if (role === 'catalog') {
    if (kind === 'readiness') { count(node, 'controller', 200); return reply({ok: true, generation: n.process}, quick(4, 10)); }
    if (releaseFails(node)) { const failure = {...typeError(node), duration_ms: quick(2, 6)}; log(node, failure); count(node, 'controller', 500); return {ok: false, http_status: 500, result: failure, duration_ms: failure.duration_ms}; }
    count(node, 'controller', n.drifted ? 500 : n.full ? 507 : 200);
    if (n.drifted) { const failure = {...sourceMissing(node), duration_ms: quick(2, 6)}; log(node, failure); return {ok: false, http_status: 500, result: failure, duration_ms: failure.duration_ms}; }
    if (n.full) { const failure = diskFull(node); log(node, failure); return {ok: false, http_status: 507, result: failure, duration_ms: quick(4, 10)}; }
    return reply(catalogue(), quick(5, 12));
  }
  // A power-capped gateway under daytime load fails most requests: timeouts, a busy upstream bulkhead, or the
  // catalogue dropping a request whose deadline had already passed.
  if (cappedGateway(node) && Math.random() < 0.85) {
    const kindOfFailure = pick3(), result = {ok: false, dependency: 'catalog-b', correlation: crypto.randomUUID(), generation: n.process, duration_ms: between(300, 1000),
      ...(kindOfFailure === 0 ? {error: 'ReadTimeout'} : kindOfFailure === 1 ? {error: 'UpstreamBusy'} : {error: 'HTTPStatusError', upstream_status: 504, upstream_error: 'DeadlineExceeded'})};
    log(node, result); count(node, 'controller', 503);
    return {ok: false, http_status: 503, result, duration_ms: Math.round((result.duration_ms + between(150, 700)) * 10) / 10};
  }
  // An overloaded gateway in a retry storm fails most requests: its queue outlasts the callers' deadlines.
  if ((storming(node) && Math.random() < 0.9) || (settling(node) && Math.random() < 0.5)) {
    const result = {ok: false, dependency: 'catalog-b', correlation: crypto.randomUUID(), error: Math.random() < 0.6 ? 'ReadTimeout' : 'UpstreamBusy', generation: n.process, duration_ms: between(900, 1000)};
    log(node, result); count(node, 'controller', 503);
    return {ok: false, http_status: 503, result, duration_ms: Math.round((result.duration_ms + between(200, 900)) * 10) / 10};
  }
  // Gateway readiness and business requests call the catalogue dependency, unless the gateway's own release fails first.
  if (releaseFails(node)) { const failure = {...typeError(node), duration_ms: quick(1, 4)}; log(node, failure); count(node, 'controller', 500); return {ok: false, http_status: 500, result: failure, duration_ms: failure.duration_ms}; }
  if (dnsBroken(node)) {
    const failure = {ok: false, dependency: 'catalog-b', correlation: crypto.randomUUID(), error: 'NameResolutionError', detail: lookupError(), generation: n.process, duration_ms: quick(0.3, 1.2)};
    log(node, failure); count(node, 'controller', 503);
    return {ok: false, http_status: 503, result: failure, duration_ms: Math.round((failure.duration_ms + between(1, 4)) * 10) / 10};
  }
  // A partitioned gateway's connection setup to the catalogue is never answered, so every call gives up after 1 second.
  if (n.partitioned) {
    const failure = {ok: false, dependency: 'catalog-b', correlation: crypto.randomUUID(), error: 'ConnectTimeout', generation: n.process, duration_ms: between(1000, 1004)};
    log(node, failure); count(node, 'controller', 503);
    return {ok: false, http_status: 503, result: failure, duration_ms: Math.round((failure.duration_ms + between(2, 6)) * 10) / 10};
  }
  const correlation = crypto.randomUUID(), cat = fleet['catalog-b'], net = call(node), catFails = releaseFails('catalog-b');
  let result, status = 503;
  const wait = answering('catalog-b') ? (cat.busy || hiccup() ? busyMs() : flooded() ? floodMs() : between(1.5, 6)) : between(1, 5);
  const upstream = answering('catalog-b') ? Math.round((wait + lag('catalog-b') + lag(node) + net.extra) * 10) / 10 : wait;
  const reached = !n.drifted && answering('catalog-b') && !net.error;
  if (reached) count('catalog-b', node, n.skewed ? 401 : cat.squeezed ? 429 : catFails || cat.drifted ? 500 : cat.full ? 507 : 200);
  if (nameGone()) result = {ok: false, dependency: 'catalog-b', correlation, error: 'NameResolutionError', detail: goneError()};
  else if (n.drifted || !answering('catalog-b')) result = {ok: false, dependency: 'catalog-b', correlation, error: 'ConnectError'};
  else if (net.error) result = {ok: false, dependency: 'catalog-b', correlation, error: net.error};
  else if (n.skewed) {
    const {error, detail} = expired();
    result = {ok: false, dependency: 'catalog-b', correlation, error: 'HTTPStatusError', upstream_status: 401, upstream_error: error, upstream_detail: detail};
    log('catalog-b', {ok: false, client: node, error, detail, skew_s: CLOCK_STEP, duration_ms: between(0.2, 0.8)});
  }
  else if (cat.squeezed) { result = {ok: false, dependency: 'catalog-b', correlation, error: 'HTTPStatusError', upstream_status: 429, upstream_error: 'QuotaExceeded'}; log('catalog-b', {...quotaRefusal(node), duration_ms: between(0.3, 1)}); }
  else if (catFails) { result = {ok: false, dependency: 'catalog-b', correlation, error: 'HTTPStatusError', upstream_status: 500, upstream_error: 'TypeError'}; log('catalog-b', typeError('catalog-b')); }
  else if (cat.drifted) { result = {ok: false, dependency: 'catalog-b', correlation, error: 'HTTPStatusError', upstream_status: 500, upstream_error: 'FileNotFoundError'}; log('catalog-b', sourceMissing('catalog-b')); }
  else if (cat.full) { result = {ok: false, dependency: 'catalog-b', correlation, error: 'HTTPStatusError', upstream_status: 507, upstream_error: 'OSError'}; log('catalog-b', diskFull('catalog-b')); }
  else if (wait > 1000) result = {ok: false, dependency: 'catalog-b', correlation, error: 'ReadTimeout'};
  else { result = {ok: true, dependency: 'catalog-b', correlation, catalog: catalogue()}; status = 200; }
  const failedAt = net.error ? net.extra : wait > 1000 ? 1000 : null;
  Object.assign(result, {generation: n.process, duration_ms: failedAt ?? upstream});
  const {catalog: _, ...logged} = result;
  log(node, logged);
  const total = Math.round((result.duration_ms + (n.busy ? busyMs() : between(2, 9))) * 10) / 10;
  if (status === 200 && n.full) { const failure = diskFull(node); log(node, failure); count(node, 'controller', 507); return {ok: false, http_status: 507, result: failure, duration_ms: total}; }
  count(node, 'controller', status);
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
const lab = {latest: new Map(), baseline: 0, successes: 0, recent: [], healthy: [], lastCycle: null, session: null};
const DETECT_FAILING = 3, DETECT_WINDOW = 5, VERIFY_STREAK = 3, FLAPPING_STREAK = 10;

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

// When the latest fix restored some of the failing services but not all, say so from the measurements: a compound
// incident's first fix is real progress, not a wrong fix.
function partialRecovery(s, results) {
  const attempt = s.recovery_attempts[s.recovery_attempts.length - 1] || {}, before = attempt.unhealthy_before || [];
  if (attempt.status !== 'succeeded' || !before.length) return '';
  const well = Object.fromEntries(NODES.map(n => [n, results.filter(e => e.node === n).every(e => e.body.ok && !e.body.slow)]));
  const restored = before.filter(n => well[n]), failing = NODES.filter(n => !well[n]);
  if (!restored.length || !failing.length) return '';
  attempt.measured ??= `partly restored: ${restored.join(', ')} healthy again; ${failing.join(', ')} still failing`;
  return `: ${restored.join(', ')} healthy again, ${failing.join(', ')} still failing. One cause may be fixed and another remain.`;
}

function cycle() {
  const results = NODES.flatMap(n => ['liveness', 'business'].map(k => check(n, k, 'collector')));
  const good = results.every(e => e.body.ok && !e.body.slow);
  const recoveredCycle = good && lab.recent.length > 0 && !lab.recent[lab.recent.length - 1];
  lab.baseline = good ? lab.baseline + 1 : 0;
  lab.successes = good ? lab.successes + 1 : 0;
  if (lab.successes <= 1) lab.healthy = [];
  if (good) lab.healthy = [...lab.healthy, results.map(e => e.id)].slice(-3);
  lab.recent = [...lab.recent, good].slice(-DETECT_WINDOW);
  lab.failing = [...(lab.failing || []), results.filter(e => !e.body.ok || e.body.slow).map(e => e.node)].slice(-DETECT_WINDOW);
  lab.lastCycle = now();
  const s = lab.session;
  if (!s || s.status === 'ended') return;
  // Intermittent faults fail some cycles and pass others, so detection counts failures in a window.
  if (s.status === 'observing' && lab.recent.filter(g => !g).length >= DETECT_FAILING) {
    s.status = 'investigating';
    s.detected_at = now();
    s.verification = 'Repeated failures observed; investigate the evidence';
  }
  // Once failures have come and gone on their own, three healthy cycles can be luck: require a longer run.
  // The first recovery after a fix may be the fix working, so it does not count; later ones do.
  if (recoveredCycle && ['observing', 'investigating', 'verifying'].includes(s.status)) {
    if (s.status === 'verifying' && !s.settled_since_fix) s.settled_since_fix = true;
    else { s.flaps = (s.flaps || 0) + 1; s.required_streak = FLAPPING_STREAK; }
  }
  if (s.rollout && ['observing', 'investigating', 'verifying'].includes(s.status)) reconcileRollout(s, results);
  if (['observing', 'investigating', 'verifying'].includes(s.status)) reconcileBrownout(results);
  const needed = s.status === 'resetting' ? VERIFY_STREAK : s.required_streak || VERIFY_STREAK;
  if (['verifying', 'resetting'].includes(s.status) && lab.successes >= needed) {
    if (s.status === 'resetting') {
      Object.assign(s, {status: 'ended', ended_at: now(), outcome: s.outcome || 'reset',
        closure: s.reset?.automatic ? 'automatic_reset' : 'operator_reset',
        verification: 'Baseline verified; this is not a targeted recovery', reset_evidence_ids: lab.healthy.flat()});
    } else {
      Object.assign(s, {status: 'recovered', outcome: 'verified_recovery', recovery_verified_at: now(),
        verification: (needed === VERIFY_STREAK ? 'Three' : String(needed)) + ' consecutive healthy fleet and business checks', recovery_evidence_ids: lab.healthy.flat()});
    }
    s.verified_at = now();
  } else if (s.status === 'verifying' && !good) {
    s.verification = 'Recovery has not restored all affected checks' + partialRecovery(s, results);
  }
  if (!['ended', 'recovered', 'resetting'].includes(s.status) && now() >= s.deadline + 60) resetLocked('expiry-' + s.id, true);
}

const HIDDEN = ['fault_target', 'fault_kind', 'fault_action', 'start_key', 'reset_key', 'extensions', 'rollout'];
// The deploy controller of an active rollout: re-apply the release to a fresh process, and restart a crashed service
// after three missed health cycles, as aegis/controller.py does.
function reconcileRollout(s, results) {
  const r = s.rollout, n = fleet[r.node], live = results.find(e => e.node === r.node && e.kind === 'liveness');
  if (live.body.ok) {
    delete r.down_cycles;
    if (live.body.result?.version !== r.version) deploy(r.node, r.version, 'deploy-controller');
    return;
  }
  if (n.running) return;
  r.down_cycles = (r.down_cycles || 0) + 1;
  if (r.down_cycles >= 3) { apply(r.node, 'start'); r.restarts = (r.restarts || 0) + 1; delete r.down_cycles; }
}
function powerDrop(node, sag) {
  if (fleet[node].running) { apply(node, 'stop'); sag.drops.push(now()); }
  sag.up = sag.down = 0;
}
// While a feed sags, the server it feeds keeps losing power: up a few cycles, down, rebooted, down again.
function reconcileBrownout(results) {
  for (const state of Object.values(facility)) {
    const sag = state.sag;
    if (!sag?.active) continue;
    const live = results.find(e => e.node === sag.node && e.kind === 'liveness');
    if (live?.body.ok) { sag.up += 1; if (sag.up >= BROWNOUT_UP) powerDrop(sag.node, sag); continue; }
    if (fleet[sag.node].running) continue;  // booting
    sag.down += 1;
    if (sag.down >= BROWNOUT_DOWN) { apply(sag.node, 'start'); sag.up = sag.down = 0; }
  }
}
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
    return {id, role: isGateway(id) ? 'gateway' : 'catalogue', health, live: live || null, business: business || null};
  });
  const history = [...sessions.values()].sort((a, b) => b.started_at - a.started_at).slice(0, 20).map(s => {
    const p = publicSession(s);
    return {id: p.id, mode: p.mode, status: p.status, started_at: p.started_at, outcome: p.outcome ?? null, closure: p.closure ?? null,
      revealed: p.revealed, recovery_verified_at: p.recovery_verified_at ?? null, notes_count: p.notes.length, help_count: p.help.length,
      evidence: evidenceSummary(s.id)};
  });
  return {nodes, session: publicSession(), baseline_cycles: lab.baseline, healthy_streak: lab.successes, collector_error: null, last_cycle: lab.lastCycle,
    evidence: lab.session ? sessionEvidence(lab.session.id, 60) : newestFirst(evidence.filter(e => !e.session_id)).slice(0, 60),
    ai: {available: false, reason: 'Not available in the browser simulator. In the full lab it is optional: add your own Gemini or Claude API key and it reads only the evidence on this page.'},
    now: t, history, storage: storage(), token: 'simulator',
    retention: {raw_hours: 24, summary_days: 30, warning: 'Export supporting evidence before 24 hours. Older references may become unavailable.'}};
}

// Docker-equivalent effects. Starting a running service or clearing a disk that is not full
// changes nothing and says so, as the real broker and services do; a stopped service cannot confirm maintenance.
const FAULTS = {'service-unavailable': ['stop', 'start'], 'cpu-starvation': ['burn', 'calm'], 'disk-full': ['fill', 'clear'], 'config-drift': ['drift', 'restore'],
  'network-delay': ['delay', 'undelay'], 'packet-loss': ['drop', 'undrop'], 'quota-zero': ['squeeze', 'requota'], 'bad-release': ['ship', 'rollback'], 'memory-leak': ['leak', 'rollback'],
  'traffic-flood': ['flood', 'ratelimit'], 'retry-storm': ['storm', 'shed'], 'power-loss': ['trip', 'breaker'], 'thermal-throttle': ['heat', 'uncap'], 'dns-failure': ['repoint', 'resolver'], 'brownout': ['sag', 'transfer'], 'partition': ['isolate', 'route'], 'clock-skew': ['skew', 'resync'],
  'heatwave-storm': ['heatwave', 'uncap']};
const FAULT_TARGETS = {'quota-zero': ['catalog-b'], 'traffic-flood': ['catalog-b'], 'retry-storm': ['gateway-a', 'gateway-c'], 'power-loss': ['catalog-b'], 'thermal-throttle': ['catalog-b'], 'dns-failure': ['gateway-a', 'gateway-c'], 'brownout': ['gateway-c'], 'partition': ['gateway-a', 'gateway-c'], 'clock-skew': ['gateway-a', 'gateway-c'], 'heatwave-storm': ['gateway-a', 'gateway-c']};  // faults that only exist on some services
// Reset normally reapplies the remedy; a flood is undone by stopping its source and removing any limit.
const RESET_CLEANUP = {'traffic-flood': ['unflood', 'requota'], 'retry-storm': ['unstorm', 'unshed'], 'thermal-throttle': ['unstorm'], 'heatwave-storm': ['unstorm', 'unshed']};
const UNCHANGED = {start: 'Already running; nothing changed', stop: 'Already stopped; nothing changed', calm: 'No background job running; nothing changed',
  burn: 'A background job was already running; nothing changed', clear: 'No filler file present; nothing changed', fill: 'Nothing changed',
  restore: 'Running config already matches the deployed settings; nothing changed', drift: 'Setting already overridden; nothing changed',
  undelay: 'No network delay present; nothing changed', delay: 'Network delay already present; nothing changed',
  undrop: 'No packet loss present; nothing changed', drop: 'Packet loss already present; nothing changed',
  requota: 'Quotas already match the deployed limits; nothing changed', squeeze: 'Quotas already at zero; nothing changed',
  rollback: 'Already running the stable release; nothing changed', ship: 'Release already deployed; nothing changed', leak: 'Release already deployed; nothing changed',
  ratelimit: 'No heavy caller without a quota to limit; nothing changed', flood: 'Export job already running; nothing changed', unflood: 'No export job running; nothing changed',
  shed: 'Load shedding already on, or not available here; nothing changed', unshed: 'Load shedding already off; nothing changed',
  storm: 'App users already running; nothing changed', unstorm: 'No app users running; nothing changed',
  breaker: 'The breaker feeding this service is already closed; nothing changed', trip: 'Breaker already open; nothing changed',
  uncap: 'No power cap is active; nothing changed', heat: 'Cooling already failed; nothing changed',
  resolver: 'Resolver already uses the nameserver it was deployed with; nothing changed', repoint: 'Resolver already repointed; nothing changed',
  transfer: "This service's rack already runs on a healthy feed; nothing changed", sag: 'Feed already sagging; nothing changed',
  route: 'No partition rule on this service; nothing changed', isolate: 'Partition rule already present; nothing changed',
  resync: 'Clock already in step with the host; nothing changed', skew: 'Clock already stepped; nothing changed',
  heatwave: 'Cooling already failed; nothing changed'};
const DEBRIEFS = {
  "service-unavailable": "The scenario requested stopping {target}. When that succeeds, its container state reads stopped and its own probes fail. When the catalogue stops, both gateways stay live but their business requests fail, so the failure spreads; once their cached answer expires they cannot even resolve its name, because Docker's resolver drops the name of a stopped container, which looks like a DNS fault until the catalogue is checked; when one gateway stops, the catalogue and the other gateway stay healthy, so it does not. Starting the stopped service restores the request path.",
  "cpu-starvation": "The scenario requested a runaway background job inside {target}. When that succeeds, the service keeps running and answering, but slowly: the job uses the container's whole 0.25-CPU limit, the kernel throttles it, and requests queue behind it, crossing the 300 ms threshold. Its log records the job starting. A slow catalogue makes both gateways slow, and sometimes time out, too. Stopping the job returns normal latency.",
  "disk-full": "The scenario requested filling {target}'s 8 MiB /tmp with one large file. When that succeeds, the process stays live, but every business request must write a small receipt, so writes fail with ENOSPC (HTTP 507). A full catalogue fails both gateways' requests too; a full gateway fails only its own. Disk usage shows 100% and the filler as the largest file. Clearing it restores writes.",
  "config-drift": "The scenario requested a bad runtime configuration reload on {target}. When that succeeds, the process stays live and its container keeps running, so liveness, container state, CPU and disk all look normal. On a gateway, the upstream address points at a port nothing listens on, so every business request fails to connect, which looks like a stopped catalogue until the catalogue is checked directly. On the catalogue, the data source points at a dataset that was never deployed, so its business requests fail with HTTP 500 and both gateways' fail with them. The running config shows the drifted setting next to its deployed value, and the log records the reload. Restoring the deployed config fixes it.",
  "packet-loss": "The scenario requested dropping 50% of the packets leaving {target}, applied with a Linux netem queue inside its own network namespace. Real links already hurt at 1 to 2% loss; the lab exaggerates it so the effect shows within a minute. When that succeeds, the process stays live and its CPU, disk and config stay normal, but TCP must resend every lost packet after a timeout of at least 200 ms. So the same request is sometimes instant, sometimes about 200 ms or 600 ms late, and sometimes fails when a lost connection setup or reply outlasts the gateways' 1-second upstream timeout. A lossy catalogue makes both gateways fail intermittently; a lossy gateway affects only itself. Because failures come and go, recovery has to hold for ten healthy cycles rather than three. The network path check shows the netem loss setting and a high share of retransmitted TCP segments, and some connection attempts take about a second while a lost handshake is resent. Removing the packet loss, or restarting the container, restores reliable requests.",
  "quota-zero": "The scenario requested a quota push that set both gateways' request quotas on {target} to zero, as a faulty quota system might. Quotas are enforced by the shared catalogue for each named caller and come from the quota system, not the service's own config. When that succeeds, the catalogue is up, fast and idle, and its own checks pass, because the controller's health probes have no quota: the monitoring is green while every gateway request is refused with HTTP 429. Both gateways stay live but fail every business request. The running config matches what was deployed, so restoring config changes nothing; the traffic check shows each gateway's requests rejected against a limit of 0 per second, and the log records the quota push. Resetting the client quotas restores service.",
  "bad-release": "The scenario requested a rollout of release v1.5.0 to {target}, whose new price-conversion code fails about half of business requests with a TypeError (HTTP 500). The process stays live and its container, CPU, disk, network and running config all look normal: nothing about the machine changed, only the code. A bad catalogue release fails about half of both gateways' requests with an upstream 500, which looks like a drifted catalogue config until the config is found unchanged; a bad gateway release affects only that gateway. Because failures come and go, recovery has to hold for ten healthy cycles. Release history shows v1.5.0 deployed moments before the errors began, and the log records each TypeError with the version. Rolling back to v1.4.2 restores service; restoring config changes nothing.",
  "memory-leak": "The scenario requested a rollout of release v1.5.1 to {target}, whose background cache warmer keeps everything it loads, about 12 MiB a second. When that succeeds, the service works normally while its memory climbs, until it reaches the container's 192 MiB limit and the kernel kills it (OOMKilled, exit code 137). The rollout's deploy controller then restarts it after a few seconds and re-applies v1.5.1, as an orchestrator would, so the service crash-loops: healthy, then down, then healthy again. Starting it by hand only begins the next loop. While it is down a stopped catalogue fails both gateways' requests and a stopped gateway only its own. The memory check shows memory growing about 12 MiB a second towards the limit and the last exit as an OOM kill; release history shows v1.5.1 re-deployed after every restart. Rolling back to v1.4.2 stops the leak and keeps it stopped.",
  "traffic-flood": "The scenario requested starting a report export job that pulls {target} as fast as 256 keep-alive connections allow, from a separate reports service on the internal network. When that succeeds, the catalogue handles several hundred requests a second at its 0.25-CPU limit, so its own checks and both gateways' requests queue behind the flood and turn slow, sometimes past the gateways' 1-second timeout. Nothing is broken or misconfigured: resource use shows the CPU at its limit, which looks like CPU starvation, but there is no runaway job, so stopping one changes nothing. The traffic check shows one caller, report-exporter, sending most of the requests without a quota. Rate-limiting it makes the catalogue answer that caller with HTTP 429 and Retry-After, the exporter backs off as a well-behaved client should, and latency returns to normal.",
  "retry-storm": "The scenario requested app users, 60 a second through {target}, whose app retries a failed request at once up to three times, and then a short index compaction that took the catalogue's whole CPU for 8 seconds. The compaction made requests time out; every timeout became up to four attempts; the extra attempts overloaded {target}, so its requests kept failing after the compaction had ended. That is a metastable failure: the overload outlives its cause, and it sits in a different service from the trigger. The other gateway recovers as soon as the compaction ends. Nothing is broken, stopped or misconfigured, and no unknown caller floods anything, so restarting, stopping a job or rate-limiting a client changes nothing. Resource use shows {target} at its CPU limit while the catalogue is idle, and its traffic shows most requests are retries. Enabling load shedding on {target} caps the requests it keeps in flight and turns the rest away at once, so the ones it accepts finish in time, users stop retrying, and the load falls back to normal.",
  "thermal-throttle": "The scenario requested a simulated failure of cooling unit CRAC-2, which raised the inlet temperature of both racks to 38 \u00b0C; the facility responded with a power cap that lowered every container's CPU limit from 0.25 to 0.05, and steady daytime users, 32 a second through each gateway, kept the services busy. The facility layer is simulated, but the cap and the users were real. A CPU cap alone barely slows these light requests; under load it does. The gateways, which do the most work per request, ran out first: both turned slow even to liveness checks and failed most business requests, while the catalogue's simple answers stayed fast. Resource use showed each gateway throttled against a limit far below its deployed 0.25, which looks like CPU starvation, but there is no job to stop and no caller to limit. The facility check showed the failed cooling unit, the hot inlet and the active cap. Lifting the cap while the room is still hot is refused, as a facilities team would refuse it; once cooling recovered after about 75 seconds, lifting the power cap restored normal latency.",
  "power-loss": "The scenario requested a simulated breaker trip on PDU-A, the power distribution unit feeding rack R1, where {target} and gateway-a are installed. The facility layer is simulated, but its effect was real: both containers were stopped, as servers are when their power is cut, and neither can be started while the breaker is open. Two services failed at the same moment, which on its own could look like two separate faults; what they share is not software but a power feed. gateway-c, on PDU-B, kept running, but its business requests fail because the catalogue is down. Starting either service reports no power and changes nothing. The facility check shows the open breaker, the rack and every service it feeds. Resetting the breaker restores power and starts the rack's services, the catalogue first because the gateways depend on it.",
  "clock-skew": "The scenario requested a time daemon on {target} stepping its clock 300 seconds ahead after it selected a bad time source. Containers share the host kernel's clock, and moving it would need CAP_SYS_TIME and would move the host, so the lab offsets the gateway process's own clock instead; everything it reports or signs with a time used the skewed clock, while deadlines were still measured on the shared kernel clock, as a relative time budget would be. Each call from a gateway to the catalogue carries a timestamp and an HMAC signature, and the catalogue refuses a timestamp more than 30 seconds from its own clock, so a replayed request cannot be used later. Once the clock jumped, every request from {target} was refused with HTTP 401 (RequestExpired), naming a timestamp 300 seconds ahead. The process, its CPU, disk, network, release and running config were all normal, and the catalogue and the other gateway stayed healthy. A 401 is not a 429: no quota was involved, so resetting quotas changed nothing, and rolling back or restoring config changed nothing because neither had changed. The catalogue's log and traffic recorded the rejected signatures and the skew it saw, the gateway's own log entries carried times in the future, and the network path check showed the gateway's clock 300 seconds ahead of the controller's and its time source. Resynchronising the clock fixed it at once; in the lab, restarting the gateway also did, because the offset lived in its process.",
  "heatwave-storm": "The scenario requested two things at once, as a heatwave can bring them: a simulated failure of cooling unit CRAC-2, with the facility's power cap lowering every container's CPU limit from 0.25 to 0.05 and steady daytime users, 32 a second through each gateway; and app users, 60 a second through {target}, whose app retries a failed request at once up to three times. The cap made both gateways fail most requests. On {target} every failure became up to four attempts, and the retries turned a capped gateway into an overloaded one: a retry storm whose trigger was the cap, not the catalogue. The facility layer is simulated; the cap and the users were real. It needed two fixes in order. Lifting the cap was refused while the room was hot, and once cooling recovered after about 75 seconds it restored the other gateway at once, which is why it was right; but {target} stayed overloaded, because the retries no longer needed the cap to sustain them. A correct first step that leaves the incident failing is still progress, not a wrong fix. Enabling load shedding on {target} then let the requests it accepted finish in time, the users stopped retrying, and only then was recovery verified. Restarting, stopping a job or rate-limiting a client changed nothing.",
  "brownout": "The scenario requested a simulated brownout on utility feed A to PDU-B, the power distribution unit feeding rack R2, where {target} is installed: the voltage sagged to about 186 V, below the 200 V its power supply tolerates. The facility layer is simulated, but its effect was real: whenever the voltage dipped, the server lost power, so the lab stopped the container, rebooted it a few seconds later, and stopped it again about ten seconds after that. A container has no power supply to lose, so it was stopped cleanly (exit code 0). The service crash-looped like a memory leak, but memory stayed flat, there was no out-of-memory kill and the release had not changed, so rolling back changed nothing; starting it only lasted until the next dip, and the breaker was closed, so resetting it changed nothing. The catalogue and gateway-a, on PDU-A, stayed healthy. The facility check showed feed A sagging, the redundant feed B normal, and a power-supply dropout at every restart. Moving the rack to the redundant feed ended the dropouts, and recovery had to hold for ten healthy cycles because the failures had come and gone.",
  "dns-failure": "The scenario requested a node-settings sync that pointed {target}'s resolver at 127.0.0.53, the address of systemd-resolved's stub on an ordinary Linux host. That setting is easily copied from a host image, but nothing listens there inside a container, so every DNS query is refused. The gateway resolves the catalogue's name with real DNS queries and caches each answer for 5 seconds, so it kept working until the cached answer expired, then failed every business request with a name resolution error. Its process, CPU, disk, release and running config were all normal, and the catalogue and the other gateway stayed healthy. A failing call to the catalogue looks like a stopped catalogue, or like config drift, which also breaks the upstream address; but the catalogue answered its own checks, the config matched what was deployed, and the network path check showed the lookup refused by 127.0.0.53 while a TCP connection to the catalogue's last known address still succeeded. Restoring the resolver fixed it at once; restarting the catalogue or restoring config changed nothing.",
  "partition": "The scenario requested a partial network partition: a filter on {target}'s own network interface that drops every packet addressed to the catalogue, and nothing else, applied by a short-lived broker helper with only NET_ADMIN, as a bad firewall or routing change would. When that succeeds, {target} stays live, its CPU, disk, config and release are normal, and its resolver still finds the catalogue's name, but every connection to the catalogue's address goes unanswered, so its business requests time out after 1 second. The catalogue answers its own checks quickly and the other gateway reaches it without trouble, so the catalogue is not down; only one path is cut. That looks like a stopped catalogue from {target}'s side, like packet loss because requests time out, or like a DNS failure; but there is no netem queue, the name resolves, and the loss is total and to one destination. The network path check on {target} shows the drop filter under a prio queue and a TCP connection to the catalogue's address timing out. Restoring the route removes the filter; restarting the catalogue, removing packet loss or restoring the resolver changes nothing.",
  "network-delay": "The scenario requested a 600 ms delay on all traffic leaving {target}, applied with a Linux netem queue inside its own network namespace. When that succeeds, the process stays live and its CPU and disk stay normal, but everything it sends waits in the queue: its own checks take about 600 ms longer. A delayed catalogue slows both gateways' requests by the same amount; a delayed gateway pays the delay twice, on its call to the catalogue and on its reply, while the catalogue and the other gateway stay fast. Requests still succeed, because each wait stays under the gateways' 1-second upstream timeout. The network path check shows TCP connections taking about 600 ms and the netem queue itself, and a request trace shows a fast handler with the time spent outside it. Removing the delay, or restarting the container, restores normal latency."
};
const debrief = s => DEBRIEFS[s.fault_kind].replaceAll('{target}', s.fault_target) + ' Injection outcome: ' + (s.fault_action?.status || 'not recorded') + '. The action record explains the exercise but was not evidence available during the investigation. Other evidence-supported investigation orders are valid.';

function apply(node, verb) {
  const target = fleet[node];
  if (verb === 'start' || verb === 'stop') {
    if ((verb === 'start') === target.running) return false;
    target.running = verb === 'start';
    if (target.running) Object.assign(target, {readyAt: now() + START_DELAY, process: 'sim-' + crypto.randomUUID(), busy: false, full: false, drifted: false, delayed: false, lossy: false, squeezed: false, unresolved: false, dnsFailsAt: null, partitioned: false, skewed: false, logs: [], traffic: [],
      version: STABLE, history: [{version: STABLE, at: now(), by: 'deployment', note: RELEASE_NOTES[STABLE]}], leakAt: null, oomKilled: false, exitCode: 0, startedAt: now()});
    else Object.assign(target, {finishedAt: now(), exitCode: 0, oomKilled: false, leakAt: null});
    return true;
  }
  if (verb === 'rollback') {
    // Ending the rollout stops the deploy controller; a service it left crashed starts again on the stable build.
    const s = lab.session, ours = s?.rollout?.node === node;
    let changed = target.running && deploy(node, STABLE, 'rollback');
    if (ours) { delete s.rollout; if (!target.running) apply(node, 'start'); changed = true; }
    return changed;
  }
  if (verb === 'heat') { if (room.capped) return false; Object.assign(room, {failedAt: now(), capped: true, liftedAt: null, daytimeStoppedAt: null}); return true; }
  // The compound incident: the same cooling failure and cap, plus app users who retry at once on one gateway.
  if (verb === 'heatwave') {
    if (room.capped) return false;
    Object.assign(room, {failedAt: now(), capped: true, liftedAt: null, daytimeStoppedAt: null});
    Object.assign(storm, {target: node, start: now(), shedAt: null, stoppedAt: null, hiccupStopped: false, capTriggered: true});
    return true;
  }
  if (verb === 'uncap') {
    if (!room.capped || (roomHot() && lab.session?.status !== 'resetting')) return false;
    Object.assign(room, {capped: false, liftedAt: now()});
    return true;
  }
  if (verb === 'unstorm' && room.failedAt != null && room.daytimeStoppedAt == null) { room.daytimeStoppedAt = now(); if (storm.start == null || storm.stoppedAt != null) return true; }
  if (verb === 'sag' || verb === 'transfer') {
    const state = facility[PDU_OF[node]] ||= {}, sag = state.sag ||= {};
    if (verb === 'sag') {
      if (sag.active) return false;
      Object.assign(sag, {active: true, at: now(), node, drops: [], transferredAt: null, up: 0, down: 0});
      state.feed = 'A';
      powerDrop(node, sag);
      return true;
    }
    if (!sag.active) return false;
    Object.assign(sag, {active: false, transferredAt: now()});
    state.feed = 'B';
    if (!fleet[sag.node].running) apply(sag.node, 'start');
    return true;
  }
  if (verb === 'trip' || verb === 'breaker') {
    const pdu = PDU_OF[node], state = facility[pdu] ||= {};
    if ((state.breaker === 'open') === (verb === 'trip')) return false;
    if (verb === 'breaker') state.breaker = 'closed';  // power first, so the starts below are not refused
    for (const member of RACKS[pdu].nodes) if (fleet[member].running !== (verb === 'breaker')) apply(member, verb === 'trip' ? 'stop' : 'start');
    if (verb === 'trip') Object.assign(state, {breaker: 'open', at: now(), closedAt: null}); else state.closedAt = now();
    return true;
  }
  // The app users run in the reports service too; the compaction that triggers the storm stops by itself.
  if (verb === 'storm') {
    if (storm.start != null && storm.stoppedAt == null) return false;
    Object.assign(storm, {target: node, start: now(), shedAt: null, stoppedAt: null, hiccupStopped: false, capTriggered: false});
    setTimeout(() => { if (storm.target === node && storm.stoppedAt == null) log('catalog-b', {ok: true, event: 'background job started', job: 'index compaction', expected_s: HICCUP_S}); }, HICCUP_AFTER * 1000);
    setTimeout(() => { if (storm.target === node && storm.stoppedAt == null) log('catalog-b', {ok: true, event: 'background job finished', job: 'index compaction'}); }, (HICCUP_AFTER + HICCUP_S) * 1000);
    return true;
  }
  if (verb === 'unstorm') { if (storm.start == null || storm.stoppedAt != null) return false; storm.stoppedAt = now(); return true; }
  if (verb === 'shed') {
    if (!isGateway(node) || !target.running || storm.shedding.has(node) || !storming(node)) return false;  // only an overloaded gateway
    storm.shedding.add(node); storm.shedAt = now();
    log(node, {ok: true, event: 'load shedding enabled', max_in_flight: 12});
    return true;
  }
  if (verb === 'unshed') { if (!storm.shedding.has(node)) return false; storm.shedding.delete(node); return true; }
  if (verb === 'calm' && node === 'catalog-b' && hiccup()) { storm.hiccupStopped = true; return true; }
  // The exporter runs in the reports service, outside the three inspected services.
  if (verb === 'flood') { if (flood.start != null && flood.stoppedAt == null) return false; Object.assign(flood, {start: now(), limitedAt: null, stoppedAt: null}); return true; }
  if (verb === 'unflood') { if (flood.start == null || flood.stoppedAt != null) return false; flood.stoppedAt = now(); return true; }
  if (verb === 'ratelimit') {
    if (node !== 'catalog-b' || !target.running || !flooded()) return false;
    flood.limitedAt = now();
    log(node, {ok: true, event: 'rate limit applied', clients: ['report-exporter'], limit_rps: 2});
    return true;
  }
  if (verb === 'requota' && node === 'catalog-b' && target.running && flood.limitedAt != null && !target.squeezed) { flood.limitedAt = null; return true; }
  if (verb === 'ship' || verb === 'leak') return target.running ? deploy(node, verb === 'ship' ? BAD_RELEASE : LEAKY_RELEASE, 'release-bot') : null;
  if (!target.running) return null;  // maintenance endpoint unreachable
  if (verb === 'isolate' || verb === 'route') {
    // The broker's partition rule: only a gateway, never beside a netem queue, and removing it only removes it.
    const on = verb === 'isolate';
    if (on && (!isGateway(node) || target.delayed || target.lossy)) return null;
    if (target.partitioned === on) return false;
    target.partitioned = on;
    return true;
  }
  if (verb === 'skew' || verb === 'resync') {
    const on = verb === 'skew';
    if ((on && !isGateway(node)) || target.skewed === on) return on && !isGateway(node) ? null : false;  // only a signing gateway's clock matters
    if (on) { target.skewed = true; target.skewedAt = wall(node); log(node, {ok: true, event: 'clock stepped', step_s: CLOCK_STEP, source: BAD_TIME_SOURCE}); }
    else { log(node, {ok: true, event: 'clock resynchronised', step_s: -CLOCK_STEP, source: 'host kernel clock'}); target.skewed = false; target.skewedAt = null; }
    return true;
  }
  if (verb === 'repoint' || verb === 'resolver') {
    const on = verb === 'repoint';
    if (!isGateway(node) || target.unresolved === on) return false;  // only a gateway resolves a name
    Object.assign(target, {unresolved: on, dnsFailsAt: on ? now() + Math.random() * DNS_TTL : null});
    log(node, on ? {ok: true, event: 'resolver settings updated', nameserver: BROKEN_NAMESERVER, pushed_by: 'node-settings-sync'} : {ok: true, event: 'resolver settings restored', nameserver: NAMESERVER});
    return true;
  }
  const field = {burn: 'busy', calm: 'busy', fill: 'full', clear: 'full', drift: 'drifted', restore: 'drifted', delay: 'delayed', undelay: 'delayed',
    drop: 'lossy', undrop: 'lossy', squeeze: 'squeezed', requota: 'squeezed'}[verb];
  const on = ['burn', 'fill', 'drift', 'delay', 'drop', 'squeeze'].includes(verb);
  if (target[field] === on || (field === 'squeezed' && node !== 'catalog-b')) return false;  // only the catalogue has quotas
  target[field] = on;
  if (field === 'squeezed') {
    target.quotaAt = now();
    log(node, on ? {ok: true, event: 'quota policy updated', pushed_by: 'quota-manager', limits_rps: {'gateway-a': 0, 'gateway-c': 0}} : {ok: true, event: 'quota policy restored', clients: ['gateway-a', 'gateway-c']});
  }
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
  const unpowered = verb === 'start' && breakerOpen(node), tooHot = verb === 'uncap' && room.capped && roomHot() && lab.session?.status !== 'resetting';
  const changed = unpowered ? false : apply(node, verb);
  const detail = unpowered ? `No power to rack ${RACKS[PDU_OF[node]].rack}: the ${PDU_OF[node]} breaker is open; nothing changed`
    : tooHot ? `Inlet still ${HOT_INLET} °C: cooling unit CRAC-2 has not recovered, and lifting the cap now would overheat the room; nothing changed` : UNCHANGED[verb];
  const body = changed === null ? {status: 'unknown', node, verb, detail: 'Inspect actual state before retrying; completion was not confirmed'}
    : {status: changed ? 'succeeded' : 'unchanged', node, verb, result: {target: node, changed}, ...(changed ? {} : {detail})};
  actions.set(key, {request, session_id: lab.session?.id ?? null, at: now(), body});
  return body;
}

const RESET_FIRST = {'power-loss': ['breaker'], 'thermal-throttle': ['uncap'], 'brownout': ['transfer'], 'heatwave-storm': ['uncap']};  // steps a reset takes before starting the services
const resetPlan = s => [...(RESET_FIRST[s.fault_kind] || []).map(v => [s.fault_target, v]), ...NODES.map(n => [n, 'start']), ...(RESET_FIRST[s.fault_kind] ? (RESET_CLEANUP[s.fault_kind] || []).map(v => [s.fault_target, v]) : RESET_CLEANUP[s.fault_kind] ? RESET_CLEANUP[s.fault_kind].map(v => [s.fault_target, v])
  : FAULTS[s.fault_kind][1] !== 'start' ? [[s.fault_target, FAULTS[s.fault_kind][1]]] : [])];

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
  const kind = data.mode === 'challenge' ? pick(Object.keys(FAULTS)) : data.kind;
  const target = data.mode === 'challenge' ? pick(FAULT_TARGETS[kind] || NODES) : data.target;
  if (!(FAULT_TARGETS[kind] || NODES).includes(target)) throw new HttpError(422, 'This fault only applies to ' + FAULT_TARGETS[kind].join(' or '));
  if (!NODES.every(n => fleet[n].running)) {
    lab.baseline = 0;
    throw new HttpError(409, 'Fleet state changed since baseline; inspect and restore it before injection');
  }
  for (const pdu of Object.keys(facility)) delete facility[pdu];  // the facility state belongs to one investigation, as on the real lab
  lab.session = {id: hex(32), mode: data.mode, status: 'observing', started_at: now(), deadline: now() + 900,
    fault_target: target, fault_kind: kind, revealed: false, help: [], notes: [], recovery_attempts: [],
    start_key: data.key, verification: 'Collecting measured effects', outcome: null};
  save(lab.session);
  lab.session.fault_action = mutate(data.key, target, FAULTS[kind][0]);
  if (ROLLOUTS[kind] && lab.session.fault_action.status === 'succeeded') lab.session.rollout = {node: target, verb: ROLLOUTS[kind][0], version: ROLLOUTS[kind][1]};
  lab.baseline = lab.successes = 0;
  lab.recent = [];
  lab.failing = [];
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
const TIMEOUTS = ['ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout'];
function explain(level) {
  const t = now(), sid = lab.session.id, inspections = ['service', 'resources', 'disk', 'config', 'trace', 'network', 'traffic', 'releases', 'memory', 'facility'];
  const fresh = new Map([...lab.latest].filter(([key, e]) => e.session_id === sid && t - e.at >= 0 && t - e.at <= (inspections.includes(key.split('|')[1]) ? 30 : 6)));
  let ids = [], message = 'There is not enough fresh evidence yet. Start with a liveness check, then compare it with a business request.';
  let suggested = {node: 'gateway-a', kind: 'liveness'};
  const healthy = (l, b) => [l, b].every(e => e.body.ok && !e.body.slow);
  const observed = GATEWAYS.filter(g => fresh.has(g + '|liveness') && fresh.has(g + '|business')).map(g => [g, fresh.get(g + '|liveness'), fresh.get(g + '|business')]);
  if (observed.length) {
    // Focus on a gateway that shows a problem; the others show how far it reaches.
    const [g, live, business] = observed.find(([, l, b]) => !healthy(l, b)) || observed[0];
    ids = [live.id, business.id];
    const result = business.body.result || {};
    const catalogue = fresh.get('catalog-b|business');
    let slowCatalogue = ['business', 'liveness'].map(k => fresh.get('catalog-b|' + k)).find(e => e) || null;
    if (slowCatalogue && !(slowCatalogue.body.ok && slowCatalogue.body.slow)) slowCatalogue = null;
    if (live.body.ok && !business.body.ok && (business.body.http_status === 507 || result.errno === 'ENOSPC')) {
      message = `${g} is live, but its business request failed with HTTP 507 (insufficient storage) from its own receipt write. Check its disk usage.`;
      suggested = {node: g, kind: 'disk'};
    } else if (live.body.ok && !business.body.ok && result.upstream_status === 507) {
      message = `${g} is live, but the catalogue answered its request with HTTP 507 (insufficient storage). The catalogue is reachable yet cannot write; check its disk usage.`;
      suggested = {node: 'catalog-b', kind: 'disk'};
    } else if (live.body.ok && !business.body.ok && business.body.http_status === 500 && result.error === 'TypeError') {
      message = `${g} is live, but its own code failed the business request with HTTP 500 (${result.error}${result.version ? ', version ' + result.version : ''}). Nothing about the machine is implied: an error from the service's own code after it was working usually follows a change. Check what changed recently in its release history.`;
      suggested = {node: g, kind: 'releases'};
    } else if (live.body.ok && !business.body.ok && result.upstream_status === 500 && result.upstream_error === 'TypeError') {
      message = `${g} is live, but the catalogue answered its request with HTTP 500 from an error in its own code (TypeError). The catalogue is reachable but its code is failing; compare its release history with its running config to see what changed.`;
      suggested = {node: 'catalog-b', kind: 'releases'};
    } else if (live.body.ok && !business.body.ok && result.upstream_status === 401) {
      const stale = result.upstream_error === 'RequestExpired';
      message = `${g} is live, but the catalogue refused its request with HTTP 401 (${result.upstream_error || 'unauthorised'}${result.upstream_detail ? ': ' + result.upstream_detail : ''}). ${stale ? `A signed request whose timestamp is too far from the catalogue's clock is refused, so a replayed one cannot be used later: one of the two clocks is wrong. Compare ${g}'s clock with the others in its network path check.` : `The catalogue does not accept ${g}'s proof of identity; compare its running config and release with the other gateway's.`}`;
      suggested = stale ? {node: g, kind: 'network'} : {node: g, kind: 'config'};
    } else if (live.body.ok && !business.body.ok && result.upstream_status === 429) {
      message = `${g} is live, but the catalogue refused its request with HTTP 429 (too many requests: ${result.upstream_error || 'quota exceeded'}). The catalogue is up and answering; it is rejecting this caller. Its own health checks may still pass, because they come from a different caller. Check the catalogue's traffic and quotas.`;
      suggested = {node: 'catalog-b', kind: 'traffic'};
    } else if (live.body.ok && !business.body.ok && result.error === 'NameResolutionError') {
      const up = catalogue?.body.ok && !catalogue.body.slow;
      message = `${g} is live, but its business request failed before reaching the catalogue: it could not resolve the catalogue's name (${result.detail || 'no address'}). A name stops resolving when the resolver is broken, and also when the service behind it is gone: Docker's resolver drops the name of a stopped container. ${up ? `The catalogue answers its own requests, so look at ${g}'s resolver in its network path.` : 'Check whether the catalogue is running before blaming the resolver.'}`;
      suggested = up ? {node: g, kind: 'network'} : {node: 'catalog-b', kind: 'service'};
    } else if (live.body.ok && !business.body.ok && (result.error === 'UpstreamBusy' || result.upstream_error === 'DeadlineExceeded')) {
      message = `${g} is live, but its business request failed because ${g} itself is backed up: ${result.error === 'UpstreamBusy' ? 'all its slots for calling the catalogue were busy' : 'its call reached the catalogue only after the deadline had passed'}. The catalogue is not the bottleneck; something is slowing the gateway. Check its own resources.`;
      suggested = {node: g, kind: 'resources'};
    } else if (live.body.ok && !business.body.ok && TIMEOUTS.includes(result.error) && catalogue?.body.ok && !catalogue.body.slow) {
      ids.push(catalogue.id);
      message = `${g} is live, but its call to the catalogue timed out (${result.error}) rather than being refused, while the catalogue answers its own business requests quickly. A timeout without a slow service means packets went missing on the way: a lost connection setup or reply. If the same request sometimes works, compare the network path of both ends.`;
      suggested = {node: 'catalog-b', kind: 'network'};
      const other = NODES.find(n => isGateway(n) && n !== g), otherLive = fresh.get(other + '|liveness'), otherBusiness = fresh.get(other + '|business');
      if (result.error === 'ConnectTimeout' && otherLive && otherBusiness && healthy(otherLive, otherBusiness)) {
        // Only this gateway cannot even open a connection to a catalogue that serves everyone else: look at its own path.
        message += ` Only ${g} is affected, so start with its own network path.`;
        suggested = {node: g, kind: 'network'};
      }
    } else if (live.body.ok && !business.body.ok && result.upstream_status === 500) {
      message = `${g} is live, but the catalogue answered its request with HTTP 500. The catalogue is reachable yet failing internally; compare its logs and running config with what was deployed.`;
      suggested = {node: 'catalog-b', kind: 'config'};
    } else if (live.body.ok && !live.body.slow && !business.body.ok && catalogue?.body.ok && !catalogue.body.slow) {
      ids.push(catalogue.id);
      message = `${g} is live but its business request fails, while the catalogue answers its own business requests quickly. The catalogue works, so the break is on ${g}'s path to it: trace one of its requests and compare its running config with its deployed settings.`;
      suggested = {node: g, kind: 'config'};
    } else if (live.body.ok && !business.body.ok && slowCatalogue) {
      ids.push(slowCatalogue.id);
      message = `${g} is live but its business request fails, and the catalogue it depends on answers slowly (${slowCatalogue.body.duration_ms} ms). Slow upstream replies make the gateway's requests time out; compare the catalogue's resource use, traffic and network path.`;
      suggested = {node: 'catalog-b', kind: 'resources'};
    } else if (live.body.ok && live.body.slow && !business.body.ok) {
      message = `${g}'s business request fails and it is slow even to liveness checks (${live.body.duration_ms} ms), which do almost no work. Something is holding the gateway itself back: check its own resources before looking further.`;
      suggested = {node: g, kind: 'resources'};
    } else if (live.body.ok && !business.body.ok) {
      message = `${g} responds to liveness checks but its business request fails. Its own process is reachable; inspect the catalogue dependency before concluding the gateway is down.`;
      suggested = {node: 'catalog-b', kind: 'service'};
    } else if (!live.body.ok) {
      message = `The ${g} liveness check failed. This does not by itself distinguish a stopped process from a connectivity problem. Inspect its container state and compare the catalogue independently.`;
      suggested = {node: g, kind: 'service'};
    } else if (live.body.slow || business.body.slow) {
      const slowNode = live.body.slow ? g : 'catalog-b';
      message = `${g}'s requests succeed but slowly (${business.body.duration_ms} ms, above ${SLOW_MS} ms). ` + (slowNode === g
        ? 'It is slow even to liveness checks, so look at its own resources.'
        : "Its liveness is quick, so the delay is probably in the catalogue it depends on; compare the catalogue's latency, resource usage and traffic.");
      suggested = {node: slowNode, kind: 'resources'};
      const cpu = fresh.get(slowNode + '|resources');
      if (cpu?.body.ok && (cpu.body.cpu_pct_of_limit || 0) < 50) {
        ids.push(cpu.id);
        message += ` Its recent CPU use is normal (${cpu.body.cpu_pct_of_limit}% of its limit), so the time is probably spent elsewhere: check its network path.`;
        suggested = {node: slowNode, kind: 'network'};
      }
    } else {
      message = 'Recent gateway liveness and business checks succeeded. These observations do not establish an active failure; compare the incident timeline before changing anything.';
      suggested = {node: 'catalog-b', kind: 'liveness'};
    }
    // The other gateway shows the boundary: a shared fault reaches both, a local one does not.
    for (const [other, otherLive, otherBusiness] of observed) {
      if (other === g || healthy(live, business)) continue;
      ids.push(otherBusiness.id);
      message += healthy(otherLive, otherBusiness)
        ? ` ${other} is healthy at the same time, so whatever is wrong is probably local to ${g} rather than the catalogue they share.`
        : ` ${other} is affected too, which points at what both gateways share: the catalogue.`;
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

// Callers sending heavily with no quota of any kind, busiest first, as aegis/guidance.py finds them.
function heavyCallers(record) {
  const result = record?.body?.result || {}, limited = new Set((result.quotas || []).map(q => q.client));
  return (result.clients || []).filter(c => (c.recent_rps || 0) > 20 && !limited.has(c.client) && c.client !== 'controller' && retryShare(c) <= 0.5).sort((a, b) => b.requests - a.requests);
}

// The share of a caller's requests that are retries: over the last 10 seconds when reported, else the last minute.
const retryShare = c => (c.recent_retries != null && c.recent_rps ? c.recent_retries / (c.recent_rps * 10) : c.requests ? (c.retries || 0) / c.requests : 0);
// The busiest caller whose requests are mostly retries, if it is sending heavily now, as aegis/guidance.py finds it.
function retryStorm(record) {
  const stormy = (record?.body?.result?.clients || []).filter(c => (c.recent_rps || 0) > 20 && retryShare(c) > 0.5);
  return stormy.length ? stormy.reduce((a, b) => (b.requests > a.requests ? b : a)) : null;
}

// A power cap and a retry storm at once: two causes, fixed in order, as aegis/guidance.py explains them.
function compoundFor(fresh) {
  const newest = kind => [...fresh.entries()].filter(([key, r]) => key.endsWith('|' + kind) && r.body.ok).map(([, r]) => r).sort((a, b) => b.at - a.at);
  const cap = newest('facility').find(r => r.body.power_cap?.active), storm = newest('traffic').find(r => isGateway(r.node) && retryStorm(r));
  if (!cap || !storm) return null;
  const g = storm.node, share = Math.round(100 * retryShare(retryStorm(storm)));
  const both = `Two causes are active at once. The facility check (simulated) shows a power cap on every rack, and ${g}'s traffic shows ${share}% of its recent requests are retries: a retry storm the cap set off. Fix them in order, and expect the first fix not to be enough: once the cap is lifted, the retries alone keep ${g} overloaded. `;
  const action = cap.body.cooling?.crac_2 === 'failed'
    ? both + `Cooling unit CRAC-2 has failed and the inlet is at ${cap.body.inlet_c} °C, so the cap cannot safely be lifted yet: check the facility again until cooling is restored, then lift the power cap, then enable load shedding on ${g} if it still fails.`
    : both + `Cooling is restored: you can try lifting the power cap first, then, if ${g} still fails with mostly retries, enable load shedding on ${g}.`;
  return [[cap.id, storm.id], action, {node: cap.node, kind: 'facility'}];
}

function remedyFor(fresh) {
  const both = compoundFor(fresh);
  if (both) return both;
  const candidates = [], contradicted = [];
  for (const [key, record] of fresh) {
    const [node, kind] = key.split('|'), body = record.body;
    if (!body.ok) continue;
    let action;
    let then = null;
    if (kind === 'service' && body.running === false && fresh.get(node + '|facility')?.body.breaker === 'open') continue;  // no power: starting cannot work
    if (kind === 'service' && body.running === false && sagging(fresh.get(node + '|facility')?.body)) continue;  // starting only lasts until the next dip
    if (kind === 'facility' && sagging(body)) {
      const feed = body.feeds[body.active_feed || 'A'], drops = (body.events || []).filter(e => e.event === 'power supply dropout').length;
      action = `The facility check (simulated) shows feed ${body.active_feed || 'A'} to ${body.pdu} sagging to ${feed.voltage_v} V, below the ${body.psu_min_v} V its power supplies need, with ${drops} power-supply dropout${drops !== 1 ? 's' : ''} on ${body.fed_services.join(' and ')}; the redundant feed is normal. Each dropout cuts the server's power, so starting the service only lasts until the next dip. You can try moving the rack to the redundant feed, then verify it stays up for ten cycles.`;
    } else if (kind === 'facility' && body.power_cap?.active && body.cooling?.crac_2 === 'failed') { action = `The facility check (simulated) for ${node}'s rack shows cooling unit CRAC-2 failed, the inlet at ${body.inlet_c} °C and a power cap active on every rack. Lifting the cap while the room is this hot would overheat it, so there is no safe fix yet: check the facility again until cooling is restored.`; then = 'facility'; }
    else if (kind === 'facility' && body.power_cap?.active) action = `The facility check (simulated) for ${node}'s rack shows cooling restored and the inlet back to ${body.inlet_c} °C, but the power cap is still active. You can try lifting the power cap, then verify latency returns to normal everywhere.`;
    else if (kind === 'resources' && body.cpu_limit && body.cpu_limit < 0.25) { action = `The resource check shows ${node}'s CPU limit at ${body.cpu_limit}, below the 0.25 it was deployed with: something has capped it, which a power or thermal cap does. Check the facility before changing anything on the service.`; then = 'facility'; }
    else if (kind === 'facility' && body.breaker === 'open') action = `The facility check (simulated) shows the ${body.pdu} breaker open: rack ${body.rack} has no power, so ${body.fed_services.join(' and ')} lost power together. Starting them cannot work without power. You can try resetting the breaker, then verify every service it feeds comes back.`;
    else if (kind === 'service' && body.running === false && body.oom_killed) { action = `The recent service-state observation reports ${node} stopped by the kernel for running out of memory (OOMKilled, exit code ${body.exit_code}). Starting it may only repeat the crash: check its memory trend and release history before choosing a fix.`; then = 'memory'; }
    else if (kind === 'service' && body.running === false) action = `The recent service-state observation reports ${node} stopped. You can try starting that selected service, then verify fresh business requests. This does not establish why it stopped.`;
    else if (kind === 'resources' && (body.cpu_pct_of_limit || 0) >= 80 && record.at - Date.parse(body.started_at) / 1000 < 15) {
      action = `The resource check shows ${node} at ${body.cpu_pct_of_limit}% of its CPU limit, but its container started only seconds earlier, and a process uses its whole limit while it boots. That alone is not starvation: check resources again in a few seconds, and its container state and memory to see why it restarted.`;
      then = 'memory';
    } else if (kind === 'resources' && (body.cpu_pct_of_limit || 0) >= 80) {
      if (node === 'catalog-b' && heavyCallers(fresh.get(node + '|traffic')).length) continue;  // the traffic check explains the CPU better
      action = `The resource check shows ${node} using ${body.cpu_pct_of_limit}% of its ${body.cpu_limit}-CPU limit, so the kernel is throttling it. Something is using that CPU: if its logs show a background job starting, you can try stopping the runaway job; ${node === 'catalog-b' ? 'if its traffic shows one caller sending most requests, rate-limit that client instead.' : 'if its traffic shows most requests are retries, callers are feeding an overload and load shedding is the fix.'} Then verify latency returns below ${SLOW_MS} ms.`;
      if (!fresh.has(node + '|traffic')) then = 'traffic';
    }
    else if (kind === 'disk' && body.result?.free_bytes != null && body.result.free_bytes < 64 * 1024) action = `Disk usage shows ${node}'s /tmp full, mostly ${body.result.largest?.[0]?.path || 'one file'}. You can try clearing its temporary files, then verify business requests write receipts again.`;
    else if (kind === 'config' && body.result?.drifted?.length) {
      const setting = body.result.settings.find(s => s.drifted);
      action = `The running config on ${node} differs from its deployed settings: ${setting.key} is ${setting.value} but ${setting.deployed} was deployed. You can try restoring the deployed config, then verify business requests succeed again.`;
    } else if (kind === 'network' && (body.result?.filters || []).some(f => f.action === 'drop')) {
      const cut = body.result.filters.find(f => f.action === 'drop'), probe = body.result.upstream_connect || {};
      const seen = probe.ok === false && probe.address ? ` A TCP connection from ${node} to ${probe.name} at ${probe.address} got no answer (${probe.error}).` : '';
      action = `Traffic leaving ${node} passes a filter that drops every packet to ${cut.dst}, the address its upstream resolves to, and nothing else.${seen} The catalogue itself is not down; only this path is cut. You can try restoring the route, then verify business requests succeed again.`;
    } else if (kind === 'network' && Math.abs(body.clock_offset_s || 0) > 5) {
      const source = body.result?.clock?.source;
      action = `The network path check shows ${node}'s clock ${body.clock_offset_s > 0 ? '+' : ''}${body.clock_offset_s} s from the controller's${source ? ', set by ' + source : ''}. Signed requests carry its time, and the catalogue refuses a timestamp more than 30 s from its own. You can try resynchronising its clock, then verify business requests succeed again.`;
    } else if (kind === 'network' && body.result?.dns?.ok === false && (body.result.dns.changed || body.result.dns.connect_last_good_ms != null)) {
      const dns = body.result.dns;
      const reachable = dns.connect_last_good_ms != null ? ` A TCP connection to its last known address, ${dns.last_good_address}, took ${dns.connect_last_good_ms} ms, so the service itself is reachable; only the name fails.` : '';
      const changed = dns.changed ? ` The resolver uses ${dns.nameserver}, set by ${dns.pushed_by}, rather than the ${dns.deployed_nameserver} it was deployed with.` : '';
      action = `${node} cannot resolve ${dns.name}: ${dns.error}.${changed}${reachable} You can try restoring the resolver, then verify business requests succeed again.`;
    } else if (kind === 'network' && body.result?.qdiscs?.some(q => q.kind === 'netem' && q.loss_pct)) {
      const queue = body.result.qdiscs.find(q => q.kind === 'netem'), tcp = body.result.tcp || {};
      const resent = tcp.retrans_pct != null ? `, and ${tcp.retrans_pct}% of the TCP segments it sent had to be resent` : '';
      action = `Traffic leaving ${node} passes a netem queue that drops ${queue.loss_pct}% of packets${resent}. You can try removing the packet loss, then verify requests succeed reliably, not just once.`;
    } else if (kind === 'traffic' && retryStorm(record)) {
      const caller = retryStorm(record), share = Math.round(100 * retryShare(caller));
      action = `${node} received ${caller.requests} requests in the last minute from ${caller.client}, and ${share}% of them were retries of requests that had just failed. Callers are retrying faster than it can serve, so the overload feeds itself even if whatever started it has stopped. You can try enabling load shedding on ${node}, then verify its requests succeed again.`;
    } else if (kind === 'traffic' && node === 'catalog-b' && heavyCallers(record).length) {  // only the catalogue rate-limits
      const top = heavyCallers(record)[0];
      action = `${node} received ${top.requests} requests in the last minute from ${top.client}, about ${top.recent_rps} a second right now, from a caller with no quota: one client is using most of its capacity. You can try rate-limiting the top client, then verify the other callers' requests are fast again.`;
    } else if (kind === 'traffic' && body.result?.quotas?.some(q => q.limit_rps === 0)) {
      const zero = body.result.quotas.filter(q => q.limit_rps === 0).map(q => q.client), rejected = (body.result.clients || []).reduce((a, c) => a + (c.rejected || 0), 0);
      action = `${node} enforces a request quota of 0 per second for ${zero.join(' and ')}, set by ${body.result.quota_source?.pushed_by || 'the quota system'} rather than its deployed ${body.result.quotas[0].deployed_rps}; ${rejected} requests were rejected in the last minute. You can try resetting the client quotas, then verify the gateways' business requests succeed.`;
    } else if (kind === 'memory' && (body.oom_killed || (body.result?.growth_mib_s || 0) >= 4)) {
      const m = body.result || {};
      const seen = (m.growth_mib_s || 0) >= 4 ? `memory growing ${m.growth_mib_s} MiB a second, now ${m.current_mib} of ${m.limit_mib} MiB` : `its last exit an out-of-memory kill (exit code ${body.exit_code})`;
      action = `The memory check shows ${node}${m.version ? ' on release ' + m.version : ''} with ${seen}. A leak returns after every restart, so starting it again will not hold. You can try rolling back the release, then verify it stays up for ten cycles.`;
    } else if (kind === 'releases' && body.result?.history?.length && body.result.running !== body.result.history[0].version) {
      const history = body.result.history;
      action = `${node} runs ${body.result.running}, deployed by ${history[history.length - 1].by} after this process started on ${history[0].version}. If its failures began with that deploy, you can try rolling back the release, then verify business requests succeed reliably.`;
    } else if (kind === 'network' && body.result?.qdiscs?.some(q => q.kind === 'netem' && q.delay_ms)) {
      const queue = body.result.qdiscs.find(q => q.kind === 'netem');
      action = `Traffic leaving ${node} passes a netem queue that adds ${queue.delay_ms} ms, and new connections take about ${body.median_connect_ms} ms. You can try removing the traffic delay, then verify its latency returns below ${SLOW_MS} ms.`;
    } else continue;
    const newer = kind === 'service' ? [...fresh].filter(([k, e]) => e.node === node && /\|(liveness|business)$/.test(k) && e.at > record.at && e.body.ok && !e.body.slow).map(([, e]) => e) : [];
    if (newer.length) contradicted.push([record, newer.reduce((a, b) => (b.at > a.at ? b : a))]);
    else candidates.push([record, action, then]);
  }
  if (candidates.length) {
    const [record, action, then] = candidates.reduce((a, b) => (b[0].at > a[0].at ? b : a));
    return [[record.id], action, {node: record.node, kind: then || record.kind}];
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
  for (const a of s.recovery_attempts) events.push({at: a.at, label: `${a.verb || 'start'} ${a.node}: ${a.status}` + (a.measured ? ` (${a.measured})` : '')});
  if (s.recovery_verified_at) events.push({at: s.recovery_verified_at, label: 'Recovery verified'});
  if (s.reset) events.push({at: s.reset.at, label: s.reset.automatic ? 'Automatic reset' : 'Reset requested'});
  if (s.ended_at) events.push({at: s.ended_at, label: 'Baseline verified, ended'});
  return {session_id: id, slow_ms: SLOW_MS, points, events: events.sort((a, b) => a.at - b.at)};
}

// ---- Routing, shaped like the FastAPI handlers ----
const nodeOk = value => NODES.includes(value);
const kindOk = value => ['liveness', 'readiness', 'business', 'logs', 'service', 'resources', 'disk', 'config', 'trace', 'network', 'traffic', 'releases', 'memory', 'facility'].includes(value);
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
    if (!['start', 'calm', 'clear', 'restore', 'undelay', 'undrop', 'requota', 'rollback', 'ratelimit', 'shed', 'breaker', 'uncap', 'resolver', 'transfer', 'route', 'resync'].includes(verb)) invalid();
    // Every service that failed in the last few cycles: a struggling service can pass one cycle by luck.
    const failing = new Set([...(lab.failing || []).flat(), ...state().nodes.filter(n => n.health !== 'healthy').map(n => n.id)]);
    const unhealthy = NODES.filter(n => failing.has(n));
    const result = mutate(d.key, d.node || 'catalog-b', verb);
    if (!s.recovery_attempts.some(a => a.key === d.key)) {
      s.recovery_attempts.push({key: d.key, at: now(), unhealthy_before: unhealthy, ...result});
      s.status = 'verifying';
      s.settled_since_fix = false;
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
  'POST ai': () => { throw new HttpError(409, 'The AI investigator is not available in the browser simulator.'); },
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
