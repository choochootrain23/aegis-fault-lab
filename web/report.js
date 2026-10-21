'use strict';
// Incident report and replay: separate debrief measures (no score) and a latency chart of the
// investigation's real observations, with a crosshair and a play/scrub replay cursor.
globalThis.AegisReport = (() => {
const SVG = 'http://www.w3.org/2000/svg';
// Series colours: validated default palette slots 1-3 (all-pairs CVD safe); failures use the reserved status red.
const SERIES = [{node:'gateway-a', label:'gateway-a business', colour:'#2a78d6'}, {node:'catalog-b', label:'catalog-b business', colour:'#eb6834'},
  {node:'gateway-c', label:'gateway-c business', colour:'#1baf7a'}];
const FAIL = '#d93025';
const H = 316, M = {top:66, right:18, bottom:34, left:76}, LANE = 14;
const el = (tag, attrs = {}, text) => { const node = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v); if (text != null) node.textContent = text; return node; };
const svg = (tag, attrs = {}, text) => { const node = document.createElementNS(SVG, tag); for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v); if (text != null) node.textContent = text; return node; };
const seconds = s => s == null ? '—' : s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
const offset = s => `+${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

function tiles(report) {
  const kinds = Object.entries(report.checks_by_kind || {}).map(([k, n]) => `${n} ${k}`).join(', ') || 'none';
  const outcome = {verified_recovery:'Verified recovery', reset:'Reset, not recovered'}[report.outcome] || 'In progress';
  const items = [
    ['Detected after', seconds(report.detected_after_s), 'from fault start to repeated failures'],
    ['First check after', seconds(report.first_check_after_s), 'your first diagnostic check'],
    ['Checks you ran', String(report.operator_checks), kinds],
    ['Help used', String(report.help_requests), report.help_requests ? 'explanations, hints or actions' : 'unaided'],
    ['Actions that changed nothing', String(report.ineffective_attempts), `of ${report.recovery_attempts} recovery actions`],
    ['Verified after', seconds(report.verified_after_s), outcome + (report.revealed ? ' · fault revealed' : '')],
  ];
  const grid = el('div', {class:'report-tiles'});
  for (const [label, value, note] of items) {
    const tile = el('div', {class:'report-tile'});
    tile.append(el('span', {class:'tile-label'}, label), el('strong', {}, value), el('small', {}, note));
    grid.append(tile);
  }
  return grid;
}

function chart(timeline, width) {
  const points = timeline.points.filter(p => p.kind === 'business');
  const failures = timeline.points.filter(p => p.ok === false);
  const times = [...timeline.points.map(p => p.at), ...timeline.events.map(e => e.at)];
  const t0 = Math.min(...times), t1 = Math.max(...times, t0 + 1);
  const plotW = width - M.left - M.right, plotH = H - M.top - M.bottom;
  const x = t => M.left + (t - t0) / (t1 - t0) * plotW;
  const yMin = 1, yMax = 3000;
  const y = ms => M.top + plotH - (Math.log10(Math.min(yMax, Math.max(yMin, ms))) - Math.log10(yMin)) / (Math.log10(yMax) - Math.log10(yMin)) * plotH;
  const root = svg('svg', {viewBox:`0 0 ${width} ${H}`, class:'report-chart', role:'img', 'aria-label':'Business request latency over the investigation, on a logarithmic scale, with failed checks and your actions marked'});
  // Recessive solid hairline grid and ticks.
  for (const tick of [1, 10, 100, 1000]) {
    root.append(svg('line', {x1:M.left, x2:width - M.right, y1:y(tick), y2:y(tick), class:'grid'}));
    root.append(svg('text', {x:M.left - 8, y:y(tick) + 4, class:'tick', 'text-anchor':'end'}, tick >= 1000 ? '1 s' : `${tick} ms`));
  }
  const span = t1 - t0, step = [5, 10, 15, 30, 60, 120, 300, 600].find(s => span / s <= 8) || 900;
  for (let s = 0; s <= span; s += step) root.append(svg('text', {x:x(t0 + s), y:H - 10, class:'tick', 'text-anchor':'middle'}, offset(s)));
  // Slow threshold: dashed because it is a reference value, not a gridline.
  root.append(svg('line', {x1:M.left, x2:width - M.right, y1:y(timeline.slow_ms), y2:y(timeline.slow_ms), class:'threshold'}));
  root.append(svg('text', {x:width - M.right, y:y(timeline.slow_ms) - 6, class:'tick', 'text-anchor':'end'}, `slow above ${timeline.slow_ms} ms`));
  // Lines break at failures: a failed request has no meaningful latency.
  for (const series of SERIES) {
    let d = '', open = false;
    for (const p of points.filter(p => p.node === series.node)) {
      if (p.ok && p.ms != null) { d += `${open ? 'L' : 'M'}${x(p.at).toFixed(1)} ${y(p.ms).toFixed(1)}`; open = true; } else open = false;
    }
    root.append(svg('path', {d, class:'series', stroke:series.colour}));
  }
  // Failures lane: one row per node above the plot, marked with a cross shape, not colour alone.
  SERIES.forEach((series, row) => {
    const ly = 14 + row * LANE;
    root.append(svg('text', {x:M.left - 8, y:ly + 4, class:'tick', 'text-anchor':'end'}, series.node));
    root.append(svg('line', {x1:M.left, x2:width - M.right, y1:ly, y2:ly, class:'grid'}));
    for (const p of failures.filter(p => p.node === series.node)) {
      const cx = x(p.at);
      root.append(svg('path', {d:`M${cx - 4} ${ly - 4}L${cx + 4} ${ly + 4}M${cx + 4} ${ly - 4}L${cx - 4} ${ly + 4}`, class:'failure', stroke:FAIL}));
    }
  });
  // Numbered event markers; the list below names them.
  timeline.events.forEach((event, index) => {
    const ex = x(event.at);
    root.append(svg('line', {x1:ex, x2:ex, y1:M.top - 6, y2:H - M.bottom, class:'event-rule'}));
    const g = svg('g', {class:'event-marker'});
    g.append(svg('circle', {cx:ex, cy:M.top - 6, r:8}), svg('text', {x:ex, y:M.top - 2.5, 'text-anchor':'middle'}, String(index + 1)));
    root.append(g);
  });
  const cursor = svg('line', {y1:4, y2:H - M.bottom, class:'cursor', visibility:'hidden'});
  const hit = svg('rect', {x:M.left, y:0, width:plotW, height:H - M.bottom, class:'hit', tabindex:'0', role:'group', 'aria-label':'Chart area. Use left and right arrow keys to move through time.'});
  root.append(cursor, hit);
  return {root, cursor, hit, x, t0, t1, points, failures, plotW};
}

function readout(timeline, at, t0) {
  const latest = node => [...timeline.points].reverse().find(p => p.node === node && p.kind === 'business' && p.at <= at);
  const done = timeline.events.filter(e => e.at <= at);
  const rows = SERIES.map(s => {
    const p = latest(s.node);
    return {series:s, value:!p ? 'no data yet' : p.ok === false ? 'failed' : `${p.ms} ms${p.slow ? ' · slow' : ''}`};
  });
  return {time:offset(Math.max(0, at - t0)), rows, event:done.length ? `${done.length}. ${done[done.length - 1].label}` : 'Before the first event'};
}

function render(container, detail, timeline) {
  container.replaceChildren();
  container.hidden = false;
  container.append(tiles(detail.report));
  if (!timeline.points.length) {
    container.append(el('p', {class:'panel-description'}, 'No observations are retained for this investigation, so there is nothing to replay.'));
    return;
  }
  const legend = el('div', {class:'report-legend'});
  for (const s of SERIES) { const key = el('span'), swatch = el('i', {class:'line-key'}); swatch.style.background = s.colour; key.append(swatch, document.createTextNode(s.label)); legend.append(key); }
  const failKey = el('span'); failKey.append(el('i', {class:'fail-key'}, '✕'), document.createTextNode('failed check')); legend.append(failKey);
  const slowKey = el('span'); slowKey.append(el('i', {class:'threshold-key'}), document.createTextNode('slow threshold')); legend.append(slowKey);
  const frame = el('div', {class:'report-frame'});
  const tooltip = el('div', {class:'report-tooltip', hidden:''});
  frame.append(tooltip);
  const controls = el('div', {class:'replay-controls'});
  const play = el('button', {type:'button', class:'button compact replay-play'}, '▶ Replay');
  const scrub = el('input', {type:'range', min:'0', max:'1000', value:'1000', 'aria-label':'Replay position'});
  const status = el('output', {class:'replay-readout', 'aria-live':'polite'});
  controls.append(play, scrub, status);
  const events = el('ol', {class:'report-events'});
  const start = Math.min(...timeline.points.map(p => p.at), ...timeline.events.map(e => e.at));
  timeline.events.forEach(e => { const li = el('li'); li.append(el('time', {}, offset(e.at - start)), document.createTextNode(' ' + e.label)); events.append(li); });
  const table = el('details', {class:'report-table'});
  table.append(el('summary', {}, 'Data table'));
  const tbl = el('table'), head = el('tr');
  for (const h of ['Time', 'Node', 'Check', 'Result', 'Latency']) head.append(el('th', {}, h));
  tbl.append(head);
  for (const p of timeline.points.slice(-300)) {
    const tr = el('tr');
    for (const v of [new Date(p.at * 1000).toLocaleTimeString(), p.node, p.kind, p.ok === false ? 'failed' : p.slow ? 'slow' : 'ok', p.ms == null ? '—' : `${p.ms} ms`]) tr.append(el('td', {}, v));
    tbl.append(tr);
  }
  table.append(tbl);
  container.append(legend, frame, controls, events, table);

  let built, playing = null, position = 1, drawnWidth = 0;
  const show = (fraction, announce) => {
    position = Math.min(1, Math.max(0, fraction));
    const at = built.t0 + position * (built.t1 - built.t0), cx = built.x(at);
    built.cursor.setAttribute('x1', cx); built.cursor.setAttribute('x2', cx); built.cursor.setAttribute('visibility', 'visible');
    const r = readout(timeline, at, built.t0);
    tooltip.replaceChildren(el('strong', {}, r.time));
    for (const row of r.rows) { const line = el('div'); const key = el('i', {class:'line-key'}); key.style.background = row.series.colour; line.append(key, el('b', {}, row.value), document.createTextNode(' ' + row.series.node)); tooltip.append(line); }
    tooltip.append(el('small', {}, r.event));
    tooltip.hidden = false;
    tooltip.style.left = `${Math.min(cx / built.root.viewBox.baseVal.width * 100, 72)}%`;
    scrub.value = String(Math.round(position * 1000));
    if (announce) status.textContent = `${r.time} · ${r.rows.map(row => `${row.series.node} ${row.value}`).join(' · ')} · ${r.event}`;
  };
  const draw = () => {
    const width = Math.max(320, Math.round(frame.clientWidth || 800));
    if (width === drawnWidth) return false;
    drawnWidth = width;
    built = chart(timeline, width);
    frame.replaceChildren(built.root, tooltip);
    built.hit.addEventListener('pointermove', event => { if (playing) return; const r = built.root.getBoundingClientRect(); show((event.clientX - r.left) / r.width * width / built.plotW - M.left / built.plotW, false); });
    built.hit.addEventListener('pointerleave', () => { if (!playing) { tooltip.hidden = true; built.cursor.setAttribute('visibility', 'hidden'); } });
    built.hit.addEventListener('keydown', event => { const step = {ArrowLeft:-.02, ArrowRight:.02}[event.key]; if (step) { event.preventDefault(); show(position + step, true); } });
    built.hit.addEventListener('focus', () => show(position, true));
    return true;
  };
  const stop = () => { if (playing) cancelAnimationFrame(playing); playing = null; play.textContent = '▶ Replay'; };
  play.addEventListener('click', () => {
    if (playing) { stop(); show(position, true); return; }
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) { show(1, true); return; }
    play.textContent = '❚❚ Pause';
    // A full replay takes twelve seconds; resuming continues from the current position.
    const begin = performance.now(), from = position >= 1 ? 0 : position;
    const tick = now => {
      const f = from + (now - begin) / 12000;
      if (f >= 1) { stop(); show(1, true); return; }
      show(f, false);
      playing = requestAnimationFrame(tick);
    };
    playing = requestAnimationFrame(tick);
  });
  scrub.addEventListener('input', () => { stop(); show(Number(scrub.value) / 1000, true); });
  draw();
  new ResizeObserver(() => { const keep = position; if (draw() && !tooltip.hidden) show(keep, false); }).observe(frame);
  status.textContent = 'Press Replay, drag the slider, or hover the chart.';
}

return Object.freeze({render});
})();
