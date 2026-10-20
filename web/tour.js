'use strict';
// Optional guided tour: a spotlight and a step card over the real interface. Nothing is clicked for the visitor.
globalThis.AegisTour = (() => {
const STEPS = [
  ['#topology', 'The live map', 'Your two services, plus the controller that checks them and the broker that can start or stop them. Edges show live state. Drag any node to rearrange the map.'],
  ['#journey', 'Suggested next', 'This card always shows a sensible next step, and the tracker shows where you are. It is a suggestion: explore in any order.'],
  ['.scenario-strip', 'Start a scenario', 'Practice lets you pick the fault and its target. Challenge picks a hidden fault for you to find. Start once the baseline is healthy.'],
  ['.checks', 'Run checks', 'Inspect the selected node: liveness, a full business request, container state, CPU use, disk and logs. Each chip shows that check\'s latest result.'],
  ['.recovery-box', 'Recovery toolbox', 'Try a fix on the selected node. An action that finds nothing to change says so, and recovery only counts after three healthy, fast cycles.'],
  ['#evidence-section', 'Evidence notebook', 'Every check lands here as a record. Open one for the raw measurement, and tick records to cite them in your diagnosis.'],
  ['.guidance', 'A little direction', 'Stuck? Ask for an explanation, a hint or one suggested action. Help is optional and recorded honestly in your report.'],
  ['#report-section', 'Incident report', 'Your debrief: how long detection, checks and recovery took, plus a replay of every request. Press Replay to watch the incident unfold.'],
];
const SEEN = 'aegis-tour-seen';
let index = -1, spot, card, previousFocus;
const seen = () => { try { return localStorage.getItem(SEEN) === '1'; } catch { return false; } };
const remember = () => { try { localStorage.setItem(SEEN, '1'); } catch { /* storage unavailable */ } };

function build() {
  spot = document.createElement('div');
  spot.className = 'tour-spot';
  card = document.createElement('div');
  card.className = 'tour-card';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'false');
  card.setAttribute('aria-labelledby', 'tour-title');
  card.innerHTML = '<span class="eyebrow" id="tour-count"></span><h2 id="tour-title"></h2><p id="tour-text"></p><div class="tour-actions"><button type="button" class="button compact" id="tour-skip">Skip tour</button><button type="button" class="button compact" id="tour-back">← Back</button><button type="button" class="button primary" id="tour-next">Next →</button></div>';
  document.body.append(spot, card);
  card.querySelector('#tour-skip').addEventListener('click', end);
  card.querySelector('#tour-back').addEventListener('click', () => go(index - 1));
  card.querySelector('#tour-next').addEventListener('click', () => (index === STEPS.length - 1 ? end() : go(index + 1)));
  document.addEventListener('keydown', event => { if (index >= 0 && event.key === 'Escape') end(); });
  addEventListener('resize', place);
  addEventListener('scroll', place, {passive: true});
}

function place() {
  if (index < 0) return;
  const target = document.querySelector(STEPS[index][0]);
  if (!target) return;
  const r = target.getBoundingClientRect(), pad = 8;
  Object.assign(spot.style, {top: `${r.top - pad}px`, left: `${r.left - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px`});
  const below = r.bottom + pad + card.offsetHeight + 16 < innerHeight;
  const top = below ? r.bottom + pad + 12 : Math.max(12, r.top - pad - card.offsetHeight - 12);
  const left = Math.min(Math.max(12, r.left), innerWidth - card.offsetWidth - 12);
  Object.assign(card.style, {top: `${Math.min(top, innerHeight - card.offsetHeight - 12)}px`, left: `${left}px`});
}

function go(next) {
  index = Math.max(0, Math.min(STEPS.length - 1, next));
  const [selector, title, text] = STEPS[index];
  card.querySelector('#tour-count').textContent = `Tour · ${index + 1} of ${STEPS.length}`;
  card.querySelector('#tour-title').textContent = title;
  card.querySelector('#tour-text').textContent = text;
  card.querySelector('#tour-back').disabled = index === 0;
  card.querySelector('#tour-next').textContent = index === STEPS.length - 1 ? 'Finish ✓' : 'Next →';
  const target = document.querySelector(selector);
  target?.scrollIntoView({block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  spot.hidden = card.hidden = false;
  place();
  setTimeout(place, 450);
  card.querySelector('#tour-next').focus({preventScroll: true});
}

function start() {
  if (!card) build();
  previousFocus = document.activeElement;
  document.body.classList.add('touring');
  go(0);
}

function end() {
  index = -1;
  spot.hidden = card.hidden = true;
  document.body.classList.remove('touring');
  remember();
  document.getElementById('tour-start')?.classList.remove('attention');
  previousFocus?.focus?.({preventScroll: true});
}

document.getElementById('tour-start')?.addEventListener('click', start);
if (!seen()) document.getElementById('tour-start')?.classList.add('attention');
return Object.freeze({start, seen});
})();
