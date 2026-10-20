'use strict';
// Interactive topology: draggable nodes whose connecting edges follow them and reflect measured state.
// Positions are fractions of the map so layouts survive resizing; they are remembered per browser.
globalThis.AegisTopology = (() => {
const EDGES = [
  {id:'edge', from:'gateway-a', to:'catalog-b', kind:'dependency'},
  {id:'edge-check-gateway-a', from:'controller', to:'gateway-a', kind:'check', node:'gateway-a'},
  {id:'edge-check-catalog-b', from:'controller', to:'catalog-b', kind:'check', node:'catalog-b'},
  {id:'edge-control-gateway-a', from:'broker', to:'gateway-a', kind:'control', node:'gateway-a'},
  {id:'edge-control-catalog-b', from:'broker', to:'catalog-b', kind:'control', node:'catalog-b'},
];
const DEFAULT = {'gateway-a':[.25,.5], 'catalog-b':[.75,.5], controller:[.5,.12], broker:[.5,.88]};
const STORE = 'aegis-topology';
const SVG = 'http://www.w3.org/2000/svg';
const map = document.getElementById('topology');
const svg = document.getElementById('graph-edges');
const tags = document.getElementById('graph-tags');
const nodes = Object.fromEntries([...map.querySelectorAll('[data-graph-node]')].map(el => [el.dataset.graphNode, el]));
const read = () => { try { return JSON.parse(localStorage.getItem(STORE)) || {}; } catch { return {}; } };
const write = value => { try { localStorage.setItem(STORE, JSON.stringify(value)); } catch { /* storage unavailable: layout lasts for this visit */ } };
const saved = read();
const positions = {...DEFAULT, ...(saved.positions || {})};
let plane = saved.plane !== false;

const marker = (id, colour) => `<marker id="${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="${colour}"/></marker>`;
svg.innerHTML = '<defs>' + marker('arrow-neutral', '#77777d') + marker('arrow-ok', '#1e8e3e') + marker('arrow-warn', '#f9ab00') +
  marker('arrow-bad', '#d93025') + marker('arrow-accent', '#1a73e8') + '</defs>';
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
    edge.tag.style.left = (x1 + x2) / 2 + 'px';
    edge.tag.style.top = (y1 + y2) / 2 + 'px';
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
  Object.assign(positions, DEFAULT);
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
  const gateway = byId['gateway-a'], measured = gateway?.business, stale = !measured || gateway.health === 'unknown';
  const dependency = EDGES[0];
  tone(dependency, stale ? '' : measured.body.ok ? 'flowing ok' : 'failed bad');
  dependency.tag.textContent = stale ? 'requests · no fresh data' : measured.body.ok ? `requests · ${measured.body.duration_ms} ms` : 'requests failing';
  // Operator recovery and reset are the operator's own actions; the hidden fault injection is never drawn.
  const s = state.session, recent = [];
  for (const attempt of s?.recovery_attempts || []) if (state.now - attempt.at < 4) recent.push(attempt.node);
  if (s?.reset && state.now - s.reset.at < 4) recent.push('gateway-a', 'catalog-b');
  for (const edge of EDGES.slice(1)) {
    if (edge.kind === 'check') {
      const health = byId[edge.node]?.health;
      tone(edge, TONE[health] || '');
      edge.tag.textContent = health && health !== 'unknown' ? `checks · ${health}` : 'checks · unknown';
    } else {
      const active = recent.includes(edge.node);
      tone(edge, active ? 'active' : '');
      edge.tag.textContent = active ? 'start sent' : 'inspect · start · stop';
    }
  }
}
return Object.freeze({update, place});
})();
