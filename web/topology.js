'use strict';
// Interactive topology: draggable nodes whose connecting edges follow them and reflect measured state.
// Positions are fractions of the map so layouts survive resizing; they are remembered per browser.
globalThis.AegisTopology = (() => {
const EDGES = [
  {id:'edge', from:'gateway-a', to:'catalog-b', kind:'dependency'},
  {id:'edge-c', from:'gateway-c', to:'catalog-b', kind:'dependency'},
  {id:'edge-check-gateway-a', from:'controller', to:'gateway-a', kind:'check', node:'gateway-a'},
  {id:'edge-check-catalog-b', from:'controller', to:'catalog-b', kind:'check', node:'catalog-b'},
  {id:'edge-control-gateway-a', from:'broker', to:'gateway-a', kind:'control', node:'gateway-a'},
  {id:'edge-control-catalog-b', from:'broker', to:'catalog-b', kind:'control', node:'catalog-b'},
  {id:'edge-check-gateway-c', from:'controller', to:'gateway-c', kind:'check', node:'gateway-c'},
  {id:'edge-control-gateway-c', from:'broker', to:'gateway-c', kind:'control', node:'gateway-c'},
];
// Wide maps put the gateways in a column beside the catalogue; narrow ones draw a tree with both gateways above it.
const WIDE = {'gateway-a':[.2,.29], 'gateway-c':[.2,.73], 'catalog-b':[.77,.51], controller:[.62,.08], broker:[.62,.93]};
const NARROW = {'gateway-a':[.26,.3], 'gateway-c':[.74,.3], 'catalog-b':[.5,.67], controller:[.5,.09], broker:[.5,.88]};
const DEFAULT = () => (map.clientWidth && map.clientWidth < 560 ? NARROW : WIDE);
const STORE = 'aegis-topology-3';  // layouts saved for the two-service map no longer fit
const SVG = 'http://www.w3.org/2000/svg';
const map = document.getElementById('topology');
const svg = document.getElementById('graph-edges');
const tags = document.getElementById('graph-tags');
const nodes = Object.fromEntries([...map.querySelectorAll('[data-graph-node]')].map(el => [el.dataset.graphNode, el]));
const read = () => { try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch { return {}; } };
const write = value => { try { localStorage.setItem(STORE, JSON.stringify(value)); } catch { /* storage unavailable: layout lasts for this visit */ } };
const saved = read();
const positions = {...DEFAULT(), ...(saved.positions || {})};
let plane = saved.plane !== false;

const marker = (id, colour) => `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="${colour}"/></marker>`;
svg.innerHTML = '<defs>' + marker('arrow-neutral', '#77777d') + marker('arrow-ok', '#137333') + marker('arrow-warn', '#f9ab00') +
  marker('arrow-bad', '#c5221f') + marker('arrow-accent', '#1967d2') + '</defs>';
for (const edge of EDGES) {
  const path = document.createElementNS(SVG, 'path');
  path.id = edge.id;
  path.classList.add('graph-edge', edge.kind);
  svg.append(path);
  const tag = document.createElement('span');
  tag.className = 'edge-tag ' + edge.kind;
  tag.id = edge.id + '-tag';
  tags.append(tag);
  Object.assign(edge, {path, tag});
}

// Packets travel each edge to show measured traffic: quick and green when healthy, slow and amber
// when degraded, and stopping halfway in red when requests fail. Hidden with reduced motion.
const still = matchMedia('(prefers-reduced-motion: reduce)');
const FLOW = {flow: {period: 1.4}, slow: {period: 4.2}, blocked: {period: 1.8}};
for (const edge of EDGES.filter(e => e.kind !== 'control')) {
  const group = document.createElementNS(SVG, 'g');
  group.classList.add('packets', edge.kind);
  const count = edge.kind === 'dependency' ? 3 : 1;
  edge.packets = Array.from({length: count}, (_, i) => {
    const dot = document.createElementNS(SVG, 'circle');
    dot.setAttribute('r', edge.kind === 'dependency' ? 4.5 : 3);
    group.append(dot);
    return {dot, t: i / count};
  });
  edge.flow = 'off';
  edge.group = group;
  svg.append(group);
}
function setFlow(edge, mode, tone) {
  edge.flow = still.matches ? 'off' : mode;
  edge.group.setAttribute('class', `packets ${edge.kind} ${edge.flow} ${tone || ''}`);
}
let last = 0;
function animate(time) {
  const dt = Math.min(0.1, (time - last) / 1000 || 0);
  last = time;
  for (const edge of EDGES) {
    if (!edge.packets || edge.flow === 'off' || (edge.kind === 'check' && !plane) || !edge.path.getAttribute('d')) continue;
    // A hidden edge (collapsed plane, small screens) has no geometry to follow.
    let length = 0;
    try { length = edge.path.getTotalLength(); } catch { /* not rendered */ }
    if (!length || edge.path.getClientRects().length === 0) continue;
    for (const packet of edge.packets) {
      packet.t = (packet.t + dt / FLOW[edge.flow].period) % 1;
      // A failing request gets halfway, then fades where it is refused.
      const along = edge.flow === 'blocked' ? Math.min(packet.t, 0.5) : packet.t;
      const point = edge.path.getPointAtLength(along * length);
      packet.dot.setAttribute('cx', point.x.toFixed(1));
      packet.dot.setAttribute('cy', point.y.toFixed(1));
      packet.dot.setAttribute('opacity', edge.flow === 'blocked' && packet.t > 0.5 ? (1 - (packet.t - 0.5) * 2).toFixed(2) : '1');
    }
  }
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

function clamp(id, [x, y]) {
  const el = nodes[id], w = map.clientWidth || 1, h = map.clientHeight || 1;
  const hx = el.offsetWidth / 2 / w + .01, hy = el.offsetHeight / 2 / h + .01;
  return [Math.min(1 - hx, Math.max(hx, x)), Math.min(1 - hy, Math.max(hy, y))];
}
function place() {
  for (const [id, el] of Object.entries(nodes)) {
    const [x, y] = clamp(id, positions[id]);
    el.style.left = x * 100 + '%';
    el.style.top = y * 100 + '%';
  }
  draw();
}
// Point where the line from a box's centre towards (dx, dy) leaves the box, plus a small gap.
function exit(box, dx, dy) {
  const length = Math.hypot(dx, dy) || 1, ux = dx / length, uy = dy / length;
  const t = Math.min(Math.abs(ux) > 1e-6 ? box.hw / Math.abs(ux) : Infinity, Math.abs(uy) > 1e-6 ? box.hh / Math.abs(uy) : Infinity) + 7;
  return [box.cx + ux * t, box.cy + uy * t];
}
function draw() {
  const origin = map.getBoundingClientRect();
  svg.setAttribute('viewBox', `0 0 ${origin.width} ${origin.height}`);
  const boxes = Object.fromEntries(Object.entries(nodes).map(([id, el]) => {
    const r = el.getBoundingClientRect();
    return [id, {cx: r.left - origin.left + r.width / 2, cy: r.top - origin.top + r.height / 2, hw: r.width / 2, hh: r.height / 2}];
  }));
  for (const edge of EDGES) {
    const a = boxes[edge.from], b = boxes[edge.to];
    const [x1, y1] = exit(a, b.cx - a.cx, b.cy - a.cy), [x2, y2] = exit(b, a.cx - b.cx, a.cy - b.cy);
    edge.path.setAttribute('d', `M${x1.toFixed(1)} ${y1.toFixed(1)}L${x2.toFixed(1)} ${y2.toFixed(1)}`);
    // Dependency edges converge on the catalogue, so their labels sit nearer the gateway end.
    const t = edge.kind === 'dependency' ? 0.4 : 0.5;
    edge.tag.style.left = x1 + (x2 - x1) * t + 'px';
    edge.tag.style.top = y1 + (y2 - y1) * t + 'px';
  }
}
function setPlane(on) {
  plane = on;
  map.classList.toggle('plane-hidden', !on);
  document.getElementById('topology-plane').setAttribute('aria-pressed', String(on));
  write({positions: saved.positions, plane});
  place();
}
function remember() {
  saved.positions = Object.fromEntries(Object.keys(nodes).map(id => [id, clamp(id, positions[id])]));
  write({positions: saved.positions, plane});
}

// Dragging: a short movement threshold keeps ordinary clicks selecting nodes.
for (const [id, el] of Object.entries(nodes)) {
  let start = null, dragging = false;
  el.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    start = {x: event.clientX, y: event.clientY};
    dragging = false;
    el.setPointerCapture(event.pointerId);
  });
  el.addEventListener('pointermove', event => {
    if (!start) return;
    if (!dragging && Math.hypot(event.clientX - start.x, event.clientY - start.y) < 5) return;
    dragging = true;
    el.classList.add('dragging');
    const r = map.getBoundingClientRect();
    positions[id] = clamp(id, [(event.clientX - r.left) / r.width, (event.clientY - r.top) / r.height]);
    el.style.left = positions[id][0] * 100 + '%';
    el.style.top = positions[id][1] * 100 + '%';
    draw();
  });
  const finish = () => {
    if (dragging) {
      remember();
      document.addEventListener('click', e => { e.stopPropagation(); e.preventDefault(); }, {capture: true, once: true});
      setTimeout(() => { dragging = false; }, 0);
    }
    start = null;
    el.classList.remove('dragging');
  };
  el.addEventListener('pointerup', finish);
  el.addEventListener('pointercancel', finish);
  el.addEventListener('keydown', event => {
    const step = {ArrowLeft:[-.03, 0], ArrowRight:[.03, 0], ArrowUp:[0, -.05], ArrowDown:[0, .05]}[event.key];
    if (!step || !event.shiftKey) return;
    event.preventDefault();
    positions[id] = clamp(id, [positions[id][0] + step[0], positions[id][1] + step[1]]);
    place();
    remember();
  });
  // Hover or focus lights up a node's connections.
  const light = on => EDGES.forEach(e => { if (e.from === id || e.to === id) { e.path.classList.toggle('lit', on); e.tag.classList.toggle('lit', on); } });
  el.addEventListener('pointerenter', () => light(true));
  el.addEventListener('pointerleave', () => light(false));
  el.addEventListener('focus', () => light(true));
  el.addEventListener('blur', () => light(false));
}
document.getElementById('topology-reset').addEventListener('click', () => {
  Object.assign(positions, DEFAULT());
  saved.positions = undefined;
  write({plane});
  place();
});
document.getElementById('topology-plane').addEventListener('click', () => setPlane(!plane));
new ResizeObserver(place).observe(map);
setPlane(plane);

const TONE = {healthy:'ok', degraded:'warn', unavailable:'bad'};
function tone(edge, value) {
  edge.path.classList.remove('ok', 'warn', 'bad', 'flowing', 'failed', 'active');
  if (value) edge.path.classList.add(...value.split(' '));
  const arrow = /\bbad\b|failed/.test(value) ? 'bad' : /\bwarn\b/.test(value) ? 'warn' : /\bok\b|flowing/.test(value) ? 'ok' : /active/.test(value) ? 'accent' : 'neutral';
  edge.path.setAttribute('marker-end', `url(#arrow-${arrow})`);
}
function update(state) {
  const byId = Object.fromEntries(state.nodes.map(n => [n.id, n]));
  // Each gateway's dependency edge shows its own measured business requests.
  for (const dependency of EDGES.filter(e => e.kind === 'dependency')) {
    const gateway = byId[dependency.from], measured = gateway?.business, stale = !measured || gateway.health === 'unknown';
    const slow = !stale && measured.body.ok && measured.body.slow;
    tone(dependency, stale ? '' : slow ? 'flowing warn' : measured.body.ok ? 'flowing ok' : 'failed bad');
    const label = stale ? 'no fresh data' : measured.body.ok ? `${measured.body.duration_ms} ms${slow ? ' · slow' : ''}` : 'failing';
    dependency.tag.textContent = map.clientWidth < 560 ? label : stale || measured.body.ok ? 'requests · ' + label : 'requests failing';
    setFlow(dependency, stale || gateway.health === 'unavailable' ? 'off' : slow ? 'slow' : measured.body.ok ? 'flow' : 'blocked', slow ? 'warn' : measured?.body.ok ? 'ok' : 'bad');
  }
  // Operator recovery and reset are the operator's own actions; the hidden fault injection is never drawn.
  const s = state.session, recent = {};
  for (const attempt of s?.recovery_attempts || []) if (state.now - attempt.at < 4) recent[attempt.node] = attempt.verb || 'start';
  if (s?.reset && state.now - s.reset.at < 4) for (const node of state.nodes) recent[node.id] = 'reset';
  for (const edge of EDGES.filter(e => e.kind !== 'dependency')) {
    if (edge.kind === 'check') {
      const health = byId[edge.node]?.health;
      // Job, disk and config remedies travel from the controller to the service's maintenance endpoint.
      const verb = !['start', 'reset', 'undelay'].includes(recent[edge.node]) && recent[edge.node];
      tone(edge, verb ? 'active' : TONE[health] || '');
      edge.tag.textContent = verb ? `${verb} sent` : health && health !== 'unknown' ? `checks · ${health}` : 'checks · unknown';
      setFlow(edge, health && health !== 'unknown' ? 'flow' : 'off', TONE[health]);
    } else {
      const verb = ['start', 'reset', 'undelay'].includes(recent[edge.node]) && recent[edge.node];
      tone(edge, verb ? 'active' : '');
      edge.tag.textContent = verb ? `${verb} sent` : 'inspect · start · stop · delay';
    }
  }
}
return Object.freeze({update, place});
})();
