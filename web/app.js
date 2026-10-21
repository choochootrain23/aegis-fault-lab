'use strict';
const $ = id => document.getElementById(id);
const refreshOrder = AegisView.latestRequest();
const citationOrder = AegisView.latestRequest();
let historySignature='';
let state, selected = 'gateway-a', busy = false, token = '', chosen = new Set(), citedLabels = new Map(), evidenceSignature = '', lastSession, connected=false, serverOffset=0, evidencePaused=false, evidenceSnapshot=[], archiveId=null, archiveCursor=null, archiveBusy=false, suggestedCheck=null;
const esc = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label = text => String(text || '').replaceAll('_', ' ').replaceAll('-', ' ');
// Service names never break at their hyphen ("catalog-" then "b"): wrap them, building text nodes rather than HTML.
const SERVICE_NAME=/\b(gateway-a|gateway-c|catalog-b)\b/;
function prose(el,text){text=String(text??'');if(el.textContent===text)return;el.replaceChildren(...text.split(SERVICE_NAME).map((part,i)=>{if(i%2===0)return part;const name=document.createElement('span');name.className='nobreak';name.textContent=part;return name;}));}
function setText(id,text){prose($(id),text);}
// Tone: true or 'error' for failures, 'ok' for confirmed good news, otherwise neutral progress.
function toast(text, tone=false) { const el=$('toast'), kind=tone===true?'error':tone||'info'; prose(el,text); el.dataset.tone=kind; el.classList.toggle('error',kind==='error'); el.hidden=false; el.classList.remove('enter'); void el.offsetWidth; el.classList.add('enter'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.hidden=true,kind==='error'?9000:6000); }
async function request(path, data) {
  const abort=new AbortController(), deadline=setTimeout(()=>abort.abort(),data?45000:5000);
  try{
    const res=await fetch('/api/'+path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-Aegis-Token':token}:{},body:data?JSON.stringify(data):undefined,signal:abort.signal});
    const body=await res.json(); if(!res.ok) throw new Error(typeof body.detail==='string'?body.detail:'Request rejected; check inputs and try again'); return body;
  }catch(error){
    if(abort.signal.aborted)throw new Error(data?'Request timed out. Completion is unknown; inspect fresh observations before retrying.':'Controller response timed out.');
    throw error;
  }finally{clearTimeout(deadline);}
}
async function action(path, data={}, message='Action recorded') {
  if(busy)return; busy=true; document.querySelector('main').setAttribute('aria-busy','true');renderControls();toast(path==='check'?'Running diagnostic check…':'Recording your request…');
  try { const result=await request(path,{...data,...(['recover','reset','extend','reveal','note','help','ai'].includes(path)?{session_id:state.session.id}:{}),...(['sessions','recover','reset','extend','reveal'].includes(path)?{key:crypto.randomUUID()}: {})}); toast(result.status==='unknown'?'Outcome uncertain. Inspect actual state before retrying.':result.status==='unchanged'?`Nothing changed: ${result.detail}`:message,result.status==='unknown'?'error':result.status==='unchanged'?'warn':'ok'); await refresh(); return result; }
  catch(e){toast(e.message,true);}finally{busy=false;document.querySelector('main').setAttribute('aria-busy','false');renderControls();}
}
function renderControls(){
 if(!state)return; const s=state.session, active=s&&s.status!=='ended';
 $('start').disabled=!AegisView.canStart(state,connected,busy);
 $('mode').disabled=busy||active;
 for(const id of ['practice-target','practice-kind']){$(id).disabled=busy||active;$(id).closest('.pick').hidden=$('mode').value==='challenge';}
 $('expiry-continue').disabled=busy||!connected; $('expiry-reset').disabled=busy||!connected;
 $('maintenance').disabled=busy||!connected;
 for(const id of ['reset','save-note','reveal'])$(id).disabled=busy||!connected||!active;
 document.querySelectorAll('[data-remedy]').forEach(b=>b.disabled=!AegisView.canRecover(s,connected,busy));
 $('extend').disabled=busy||!connected||!active||['resetting','recovered'].includes(s.status);
 document.querySelectorAll('[data-check]').forEach(b=>b.disabled=busy||!connected);
 document.querySelectorAll('[data-help]').forEach(b=>b.disabled=busy||!connected||!active);
 $('clear-selection').disabled=chosen.size===0;
 $('suggested-check').disabled=busy||!connected;
 $('reveal').hidden=!s||s.mode!=='challenge'||s.revealed||s.status==='ended';
 renderAI();
}
function renderEvidence(force=false){
 if(evidencePaused&&!force){const unseen=state.evidence.filter(e=>!evidenceSnapshot.some(old=>old.id===e.id)).length;$('evidence-count').textContent=state.evidence.length;$('evidence-status').textContent=`Reading paused · ${unseen} newer observation${unseen===1?'':'s'} waiting. The list is frozen until you resume.`;return;}
 if(!evidencePaused)evidenceSnapshot=state.evidence;
 const evidence=evidenceSnapshot.filter(e=>$('filter').value==='all'||e.node===$('filter').value);
 const sig=evidence.map(e=>e.id).join(','); if(sig===evidenceSignature)return; evidenceSignature=sig;
 const open=new Set([...document.querySelectorAll('.evidence-row details[open]')].map(e=>e.dataset.id));
 $('evidence-count').textContent=state.evidence.length;
 $('evidence').innerHTML=evidence.length?evidence.map(e=>`<div class="evidence-row"><input type="checkbox" aria-label="Cite ${esc(e.node)} ${esc(label(e.kind))} evidence ${esc(e.id)} in your diagnosis" title="Cite in your diagnosis" data-evidence="${esc(e.id)}" ${chosen.has(e.id)?'checked':''}><details data-id="${esc(e.id)}" ${open.has(e.id)?'open':''}><summary><i class="dot ${AegisView.evidenceResult(e).tone}"></i><strong>${esc(e.node)}</strong><span class="kind">${esc(label(e.kind))} · ${e.body.origin==='operator'?'your check':'collector'} · ${esc(AegisView.evidenceResult(e).text)}</span><time>${new Date(e.at*1000).toLocaleTimeString()}</time></summary><p class="evidence-id">Evidence ${esc(e.id)}</p>${detailView(e)}<pre tabindex="0" aria-label="Raw observation">${esc(JSON.stringify(e.body,null,2))}</pre></details></div>`).join(''):'<div class="empty">No matching evidence yet.<br><small>Choose a diagnostic check to gather observations.</small></div>';
 document.querySelectorAll('[data-evidence]').forEach(el=>el.addEventListener('change',()=>{setEvidencePaused(true); if(el.checked&&chosen.size>=20){el.checked=false;toast('Cite up to 20 evidence records in one diagnosis.',true);return;}const id=el.dataset.evidence,record=evidenceSnapshot.find(e=>e.id===id);if(el.checked){chosen.add(id);citedLabels.set(id,record?`${record.node} ${label(record.kind)}`:`evidence ${id}`);if(!renderCited.hinted){renderCited.hinted=true;toast('Cited in your diagnosis below. The notebook paused so the list holds still.');}}else{chosen.delete(id);citedLabels.delete(id);}renderCited();$('clear-selection').disabled=chosen.size===0;}));
}
// Readable views of structured results, shown above the raw JSON.
function detailView(e){
 const result=e.body?.result;
 if(e.kind==='trace'&&Array.isArray(result?.spans)){
  const total=Math.max(e.body.duration_ms||0,1), outside=e.body.outside_handler_ms||0;
  const rows=[{name:'outside handler · connection, queueing',start:0,ms:outside,tone:'outside'},
   ...result.spans.map(s=>({name:s.name,start:outside+s.start_ms,ms:s.ms,tone:s.ok?'ok':'bad',note:s.error||(s.status&&s.status!==200?'HTTP '+s.status:'')}))];
  const slowest=rows.reduce((a,b)=>b.ms>a.ms?b:a);
  return `<div class="waterfall"><p class="waterfall-head">Request trace · ${esc(total)} ms round trip${result.request_ok?'':` · failed at ${esc(result.failed_at)}`}</p>${rows.map(r=>{
   const x=Math.min(99,r.start/total*100), w=Math.max(0.8,Math.min(100-x,r.ms/total*100));
   return `<div class="wf-row ${r.tone}${r===slowest&&r.ms>0?' slowest':''}"><span class="wf-name">${esc(r.name)}${r.note?` · <b>${esc(r.note)}</b>`:''}</span><svg viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true"><rect class="wf-track" width="100" height="10"/><rect class="wf-bar" x="${x.toFixed(2)}" width="${w.toFixed(2)}" height="10"/></svg><span class="wf-ms">${esc(r.ms)} ms</span></div>`;}).join('')}</div>`;
 }
 if(e.kind==='network'&&Array.isArray(e.body.connect_ms)){
  const queues=result?.qdiscs||[];
  return `<div class="waterfall"><p class="waterfall-head">TCP connect from the controller · ${e.body.connect_ms.map(ms=>esc(ms)+' ms').join(' · ')}</p></div><table class="config-table"><thead><tr><th>Interface</th><th>Queue</th><th>Added delay</th></tr></thead><tbody>${queues.length?queues.map(q=>`<tr class="${q.kind==='netem'?'drifted':''}"><td>${esc(q.dev)}</td><td>${esc(q.kind)}</td><td>${q.delay_ms!=null?esc(q.delay_ms)+' ms':'none'}</td></tr>`).join(''):'<tr><td colspan="3">Queue settings unavailable</td></tr>'}</tbody></table>`;
 }
 if(e.kind==='config'&&Array.isArray(result?.settings)){
  return `<table class="config-table"><thead><tr><th>Setting</th><th>Running</th><th>Deployed</th></tr></thead><tbody>${result.settings.map(s=>`<tr class="${s.drifted?'drifted':''}"><td>${esc(s.key)}</td><td>${esc(s.value)}${s.drifted?' <b>≠</b>':''}</td><td>${esc(s.deployed)}</td></tr>`).join('')}</tbody></table>`;
 }
 return '';
}
function setEvidencePaused(paused){evidencePaused=paused;$('evidence-section').classList.toggle('paused',paused);$('evidence-tag').textContent=paused?'Paused':'Live';$('evidence-live').textContent=paused?'Resume live updates':'Pause updates';$('evidence-live').setAttribute('aria-pressed',String(!paused));$('evidence-status').textContent=paused?'Reading paused · the list is frozen so your rows stay put':'Following new observations';if(!paused){evidenceSignature='';renderEvidence();}}
let lastStatus;
function render(){
 const s=state.session;
 if(s&&lastSession===s.id&&lastStatus&&lastStatus!=='recovered'&&s.status==='recovered')celebrate();
 lastStatus=s?.status;
 if(lastSession!==s?.id){chosen.clear();lastSession=s?.id;evidencePaused=false;evidenceSignature='';setEvidencePaused(false);$('help-result').hidden=true;$('help-next').hidden=true;suggestedCheck=null;document.querySelectorAll('[data-help]').forEach(x=>x.classList.remove('chosen'));renderCited();}
 const unknown=state.nodes.every(n=>n.health==='unknown');
 const active=s&&s.status!=='ended';
 const baselineFailed=!active&&state.nodes.some(n=>n.health!=='healthy');
 $('notice').classList.toggle('error',!!state.collector_error||unknown||baselineFailed);
 setText('notice',state.collector_error|| (unknown?'Fleet measurements unavailable. Start the project containers; unknown is not healthy.':baselineFailed?'Baseline checks are failing. Start the project containers and inspect the failed observations before beginning.':active?'Investigation active. Checks reflect real service responses; choose your own path.':s?'Investigation ended. Review its evidence or start a new scenario once the baseline is healthy.':'Baseline collection is live. Five healthy cycles are required before a controlled fault.'));
 $('baseline').textContent=`${Math.min(state.baseline_cycles,5)}/5 healthy baseline cycles`;
 $('session-status').textContent=s?label(s.status):'Baseline collection';
 $('session-dot').className='dot '+(s?.status==='recovered'?'green':s&&s.status!=='ended'?'amber':'');
 prose($('verification'),s?.verification||'A successful control action is followed by three healthy verification cycles.');
 for(const n of state.nodes){const el=$('node-'+n.id); const badge=el.querySelector('.node-state');
 // Pulse once when measured health changes so the operator notices it without constant motion.
 if(el.dataset.health&&el.dataset.health!==n.health){el.classList.remove('changed');void el.offsetWidth;el.classList.add('changed');}
 el.dataset.health=n.health;badge.textContent=label(n.health);badge.className='node-state '+n.health;
 el.querySelector('.node-measure').textContent=AegisView.measurement(n);}
 const gateways=state.nodes.filter(n=>n.role==='gateway'&&n.business&&n.health!=='unknown'), failing=gateways.filter(n=>!n.business.body.ok).length;
 AegisTopology.update(state);
 renderJourney();renderCheckResults();renderScenario();
 $('edge-label').textContent=!gateways.length?'Waiting for fresh requests':failing?`Business requests failing on ${failing} of ${gateways.length} gateways`:'Observed business requests succeeded';
 prose($('scenario-reveal'),s?.scenario?`Known scenario: ${label(s.scenario.kind)} on ${s.scenario.target}`:'');
 prose($('debrief'),s?.debrief||'');
 const attempts=(s?.recovery_attempts||[]).map(a=>({at:a.at,title:`${AegisView.remedyLabel(a.verb||'start')} · ${a.node}`,detail:a.status==='succeeded'?'Action accepted; consult verification':a.detail||a.status}));
 if(s?.reset)attempts.push({at:s.reset.at,title:s.reset.automatic?'Timer expiry reset':'Operator reset',detail:s.status==='ended'?'Baseline verified; investigation ended':'Waiting for healthy baseline; inspect unresolved failures'});
 if(s?.verified_at)attempts.push({at:s.verified_at,title:'Verification complete',detail:s.verification});
 $('action-history').innerHTML=attempts.length?attempts.sort((a,b)=>b.at-a.at).map(a=>`<div class="history-item"><strong>${esc(a.title)}</strong><span>${new Date(a.at*1000).toLocaleTimeString()}</span><span>${esc(a.detail)}</span></div>`).join(''):'<p class="panel-description">No recovery attempts yet. Freely investigate before acting.</p>';
 $('saved-notes').innerHTML=(s?.notes||[]).slice(-3).map(n=>`<div class="saved-note">${esc(n.explanation)}<small>Uncertainty: ${esc(n.uncertainty||'Not recorded')} · ${n.evidence_ids.length} cited observations</small></div>`).join('');
 const nextHistory=JSON.stringify(state.history.map(({evidence,...summary})=>summary));
 if(nextHistory!==historySignature){
   const focused=document.activeElement?.dataset?.archive;historySignature=nextHistory;
   $('history').innerHTML=state.history.length?state.history.map(h=>`<div class="history-item"><strong>${esc(h.mode)} investigation</strong><span>${new Date(h.started_at*1000).toLocaleString()}</span><span>${esc(label(h.outcome||h.status))} · ${h.notes_count} notes · ${h.help_count} help requests${h.revealed?' · revealed':''}</span><button class="button compact" data-archive="${esc(h.id)}">Review ↗</button></div>`).join(''):'<p class="panel-description">No investigations yet. Start with a healthy baseline.</p>';
   document.querySelectorAll('[data-archive]').forEach(b=>b.addEventListener('click',()=>openArchive(b.dataset.archive)));
   if(focused)$('history').querySelector(`[data-archive="${CSS.escape(focused)}"]`)?.focus({preventScroll:true});
 }
 const usage=state.storage;$('storage-status').textContent=usage?`Evidence storage · ${(usage.live_bytes/1048576).toFixed(1)} / ${(usage.live_limit_bytes/1048576).toFixed(0)} MiB · detailed evidence retained 24h`:'Local evidence storage';
 renderEvidence();renderControls();renderTimer();renderReport();
}
// The report follows the current investigation, or the most recent one, and never redraws under the reader.
let reportKey='', reportBusy=false;
async function renderReport(){
 const s=state.session, id=s?.id||state.history[0]?.id;
 if(!id){$('report').hidden=true;setText('report-badge','No investigation yet');return;}
 const live=s&&s.id===id&&s.status!=='ended';
 const key=id+'|'+(live?Math.floor(Date.now()/5000):'final');
 const reading=$('report').matches(':hover')||$('report').contains(document.activeElement);
 if(key===reportKey||reportBusy||(reading&&reportKey.startsWith(id)))return;
 reportBusy=true;
 try{
  const [detail,timeline]=await Promise.all([request('investigations/'+encodeURIComponent(id)),request('investigations/'+encodeURIComponent(id)+'/timeline')]);
  reportKey=key;AegisReport.render($('report'),detail,timeline);
  if(detail.session.debrief){const p=document.createElement('p');p.className='report-debrief';prose(p,detail.session.debrief);$('report').append(p);}
  setText('report-badge',live?'Live · updating':label(detail.session.outcome||detail.session.status));
  setText('report-intro',live?'This investigation is still running; the replay grows as new observations arrive.':'Replay of your most recent investigation. Older ones are under Investigation history.');
 }catch(e){/* the next poll retries */}finally{reportBusy=false;}
}
function renderScenario(){
 const s=state?.session, active=s&&s.status!=='ended';
 const copy=active?(s.scenario?AegisView.scenarioCopy(s.scenario.kind,s.scenario.target,'practice'):AegisView.scenarioCopy(null,null,'challenge'))
  :AegisView.scenarioCopy($('practice-kind').value,$('practice-target').value,$('mode').value);
 if(active&&s.mode==='challenge'&&s.scenario)copy.eyebrow=copy.eyebrow.replace(/^Scenario \d+/,'Challenge · revealed');
 setText('scenario-number',copy.number);setText('scenario-eyebrow',copy.eyebrow);setText('scenario-text',copy.text);
 const [lead,em,tail]=copy.title, sig=copy.title.join('|');
 if($('scenario-title').dataset.sig!==sig){const mark=document.createElement('em');mark.textContent=em;$('scenario-title').replaceChildren(lead,mark,tail);$('scenario-title').dataset.sig=sig;}
}
function renderJourney(){
 const j=AegisView.journey(state,connected);
 $('journey').dataset.tone=j.tone;
 document.querySelectorAll('#steps li').forEach((li,i)=>{li.classList.toggle('done',i<j.step);li.classList.toggle('current',i===j.step);if(i===j.step)li.setAttribute('aria-current','step');else li.removeAttribute('aria-current');});
 const title=$('next-title');
 if(title.textContent!==j.title){title.classList.remove('fresh');void title.offsetWidth;title.classList.add('fresh');}
 setText('next-title',j.title);setText('next-detail',j.detail);
 const meter=$('meter');meter.hidden=!j.meter;
 if(j.meter){const sig=j.meter.done+'/'+j.meter.total;if(meter.dataset.sig!==sig){meter.dataset.sig=sig;meter.innerHTML=Array.from({length:j.meter.total},(_,i)=>`<i class="${i<j.meter.done?'filled':''}"></i>`).join('')+`<span>${sig} healthy cycles</span>`;}}
}
function renderCheckResults(){
 if(!state)return;
 document.querySelectorAll('[data-check]').forEach(b=>{const out=b.querySelector('.check-result');const record=state.evidence.find(e=>e.node===selected&&e.kind===b.dataset.check);const summary=AegisView.checkSummary(record);out.textContent=summary.text;out.className='check-result '+summary.tone;});
}
function renderTimer(){
 const timer=AegisView.timer(state?.session,Date.now()/1000+serverOffset),dialog=$('expiry-dialog');
 if(!timer.running){$('timer').textContent='No active timer';$('timer').classList.remove('timer-warning');$('extend').classList.remove('attention');if(dialog.open)dialog.close();return;}
 $('timer').classList.toggle('timer-warning',timer.warning||timer.expired);
 $('extend').classList.toggle('attention',timer.warning||timer.expired);
 $('timer').textContent=timer.expired?'Time expired':`${Math.floor(timer.remaining/60)}:${String(timer.remaining%60).padStart(2,'0')} remaining${timer.warning?' · extend soon':''}`;
 if(timer.expired){setText('grace-text',`Automatic reset in ${timer.grace} seconds if you do not continue.`);if(!dialog.open&&!$('archive-dialog').open)dialog.showModal();}else if(dialog.open)dialog.close();
}
let outage=0;
const retryDelay=()=>Math.min(16000,2000*2**Math.min(outage-1,3));
async function refresh(){const sequence=refreshOrder.begin();try{const next=await request('state');if(!refreshOrder.current(sequence))return;state=next;connected=true;serverOffset=state.now-Date.now()/1000;token=state.token;if(outage){toast('Reconnected to the controller. Measurements are live again.','ok');outage=0;}render();}catch(e){if(!refreshOrder.current(sequence))return;connected=false;outage++;if(state)renderControls();renderJourney();$('notice').classList.add('error');$('notice').textContent=`Controller unavailable. Displayed measurements may be stale. ${e.message} Retrying in ${Math.round(retryDelay()/1000)} s.`;document.querySelectorAll('.node-state').forEach(e=>{e.textContent='Unknown';e.className='node-state unknown';});}}
document.querySelectorAll('[data-node]').forEach(b=>b.addEventListener('click',()=>{selected=b.dataset.node;document.querySelectorAll('[data-node]').forEach(n=>{n.classList.toggle('selected',n===b);n.setAttribute('aria-pressed',String(n===b));});$('inspector-title').textContent=selected;renderCheckResults();}));
document.querySelectorAll('[data-check]').forEach(b=>b.addEventListener('click',async()=>{if(busy)return;b.classList.add('running');try{await action('check',{node:selected,kind:b.dataset.check},'Fresh observation added to the evidence notebook');}finally{b.classList.remove('running');}}));
$('start').addEventListener('click',()=>action('sessions',{mode:$('mode').value,target:$('practice-target').value,kind:$('practice-kind').value},'Investigation started. Observe the measured effects.'));
document.querySelectorAll('[data-remedy]').forEach(b=>b.addEventListener('click',()=>action('recover',{node:selected,verb:b.dataset.remedy},`${AegisView.remedyLabel(b.dataset.remedy)} requested on ${selected}. Watch the verification checks.`)));
$('reset').addEventListener('click',()=>action('reset',{},'Reset requested. History is preserved.'));
$('extend').addEventListener('click',()=>action('extend',{},'Investigation extended by 15 minutes'));
$('expiry-continue').addEventListener('click',()=>action('extend',{},'Continue exploring for 15 minutes'));
$('expiry-reset').addEventListener('click',()=>action('reset',{},'Reset requested. History is preserved.'));
$('reveal').addEventListener('click',()=>action('reveal',{},'Fault revealed. This is no longer a blind attempt.'));
$('filter').addEventListener('change',()=>{evidenceSignature='';renderEvidence(true);});
const HELP_KIND={evidence:'01 · What the evidence shows',hint:'02 · Hint · where to look next',action:'03 · Suggested action'};
document.querySelectorAll('[data-help]').forEach(b=>b.addEventListener('click',async()=>{
 const result=await action('help',{level:b.dataset.help},'Assistance recorded');
 if(!result)return;
 document.querySelectorAll('[data-help]').forEach(x=>x.classList.toggle('chosen',x===b));
 $('help-kind').textContent=HELP_KIND[b.dataset.help];prose($('help-text'),result.message);
 $('help-result').querySelectorAll('.help-followup').forEach(e=>e.remove());
 const box=$('help-result');box.hidden=false;box.classList.remove('fresh');void box.offsetWidth;box.classList.add('fresh');box.scrollIntoView({block:'nearest',behavior:'smooth'});
 suggestedCheck=result.suggested_check;$('help-next').hidden=!suggestedCheck;
 if(suggestedCheck)$('suggested-check').textContent=`Run ${checkName(suggestedCheck.kind)} · ${suggestedCheck.node} ↗`;
 showCited(result);
}));
// Cited service checks may have left the recent polling window. Keep their
// exact retained records visible instead of silently missing the highlight.
function showCited(result){
 if(!result.evidence_ids?.length)return;
 const records=new Map([...(result.evidence||[]),...state.evidence].map(e=>[e.id,e]));
 evidenceSnapshot=[...records.values()].sort((a,b)=>b.at-a.at);
 $('filter').value='all';evidenceSignature='';setEvidencePaused(true);renderEvidence(true);
 for(const id of result.evidence_ids){const detail=document.querySelector(`details[data-id="${CSS.escape(id)}"]`);if(detail){detail.open=true;detail.closest('.evidence-row').classList.add('highlight');}}
}
// The optional AI investigator: read-only, and only when a provider key is configured.
function renderAI(){
 const ai=state.ai||{}, s=state.session, active=s&&s.status!=='ended';
 setText('ai-badge',ai.available?`ON · ${ai.provider}`:'OFF');$('ai-badge').className='badge '+(ai.available?'blue':'neutral');$('ai-panel').classList.toggle('ready',!!ai.available);
 setText('ai-status',ai.available?`${ai.model}. ${ai.reason}${active?'':' Start an investigation to ask.'}`:ai.reason||'Not configured.');
 $('ai-ask').disabled=!ai.available||!active||busy||!connected;$('ai-question').disabled=!ai.available;
}
$('ai-form').addEventListener('submit',async e=>{
 e.preventDefault();
 $('ai-answer').hidden=false;$('ai-answer').classList.add('thinking');$('ai-answer').textContent='Reading the evidence…';
 const result=await action('ai',{question:$('ai-question').value},'AI answer recorded with your help requests');
 $('ai-answer').classList.remove('thinking');
 if(!result){$('ai-answer').hidden=true;return;}
 prose($('ai-answer'),result.answer);
 const meta=document.createElement('small');meta.textContent=`${result.provider} · ${result.model} · ${result.evidence_ids.length} cited observations · check what it cites`;$('ai-answer').append(meta);
 $('ai-question').value='';showCited(result);
});
const checkName=kind=>document.querySelector(`[data-check="${CSS.escape(kind)}"] strong`)?.textContent||label(kind);
// Running the suggested check must visibly answer it: select that node, show the new record and say what it found.
$('suggested-check').addEventListener('click',async()=>{
 const check=suggestedCheck;if(!check)return;
 const result=await action('check',check,'Suggested check completed. Its record is highlighted in the evidence notebook.');
 if(!result)return;
 suggestedCheck=null;$('help-next').hidden=true;
 $('node-'+check.node)?.click();
 const record=state.evidence.find(e=>e.node===check.node&&e.kind===check.kind);
 if(!record)return;
 showCited({evidence_ids:[record.id],evidence:[record]});
 const done=document.createElement('p');done.className='help-followup';
 prose(done,`Done: ${checkName(check.kind)} on ${check.node} reads “${AegisView.evidenceResult(record).text}”. Ask for help again to include it.`);
 $('help-text').after(done);
});
// What the ticked notebook records will be saved with, so the checkbox's purpose is visible where it matters.
function renderCited(){
 for(const id of citedLabels.keys())if(!chosen.has(id))citedLabels.delete(id);
 $('selected-count').textContent=chosen.size+' cited';$('clear-selection').disabled=chosen.size===0;
 const list=$('cited-list');list.replaceChildren();
 if(!chosen.size){list.textContent='No evidence cited yet. Tick the Cite box on records in the evidence notebook to back up your reasoning (optional).';list.classList.add('empty-cite');return;}
 list.classList.remove('empty-cite');const head=document.createElement('span');head.textContent='Citing:';list.append(head);
 for(const id of chosen){const chip=document.createElement('span');chip.className='cited-chip';chip.textContent=citedLabels.get(id)||`evidence ${id}`;list.append(chip);}
}
renderCited();
$('clear-selection').addEventListener('click',()=>{chosen.clear();renderCited();evidenceSignature='';renderEvidence(true);});

$('note-form').addEventListener('submit',async e=>{e.preventDefault();const saved=await action('note',{explanation:$('explanation').value,uncertainty:$('uncertainty').value,evidence_ids:[...chosen]},'Diagnosis saved with evidence references');if(saved){$('explanation').value='';$('uncertainty').value='';chosen.clear();renderCited();evidenceSignature='';renderEvidence(true);}});
$('expiry-dialog').addEventListener('cancel',e=>e.preventDefault());
$('mode').addEventListener('change',()=>{renderControls();renderScenario();});
for(const id of ['practice-kind','practice-target'])$(id).addEventListener('change',renderScenario);
$('evidence-live').addEventListener('click',()=>setEvidencePaused(!evidencePaused));
$('evidence').addEventListener('toggle',e=>{if(e.target.tagName==='DETAILS'&&e.target.open)setEvidencePaused(true);},true);
$('maintenance').addEventListener('click',()=>action('maintenance',{},'Retention cleanup complete. Active investigation evidence is preserved.'));
$('archive-close').addEventListener('click',()=>$('archive-dialog').close());
function archiveRecord(e){return `<details class="archive-record"><summary><strong>${esc(e.node)}</strong> · ${esc(label(e.kind))} · ${esc(AegisView.evidenceResult(e).text)} <time>${new Date(e.at*1000).toLocaleString()}</time></summary><p class="evidence-id">${esc(e.id)}</p>${detailView(e)}<pre tabindex="0" aria-label="Raw observation">${esc(JSON.stringify(e.body,null,2))}</pre></details>`;}
function archiveCitations(ids){return ids.length?ids.map(id=>`<button class="button compact citation" data-citation="${esc(id)}" title="Open this exact observation">↗ ${esc(id.slice(0,8))}</button>`).join(' '):'None recorded';}
async function openCitation(id,button){
 const session=archiveId, sequence=citationOrder.begin();button.disabled=true;
 try{const item=await request('investigations/'+encodeURIComponent(session)+'/evidence/'+encodeURIComponent(id));if(archiveId!==session||!citationOrder.current(sequence))return;
 $('archive-citation').innerHTML=archiveRecord(item);$('archive-citation').hidden=false;
 const detail=$('archive-citation').querySelector('details');detail.open=true;detail.querySelector('summary').focus();$('archive-citation').scrollIntoView({block:'nearest'});
 }catch(e){if(citationOrder.current(sequence))toast(e.message,true);}finally{button.disabled=false;}
}
async function openArchive(id){
 if(archiveBusy)return;archiveBusy=true;archiveId=id;archiveCursor=null;$('archive-citation').replaceChildren();$('archive-citation').hidden=true;$('archive-report').replaceChildren();$('archive-evidence').replaceChildren();$('archive-notes').replaceChildren();$('archive-warning').hidden=true;$('archive-more').disabled=true;$('archive-title').textContent='Loading investigation…';$('archive-summary').textContent='';$('archive-export').removeAttribute('href');if(!$('archive-dialog').open)$('archive-dialog').showModal();
 try{const detail=await request('investigations/'+encodeURIComponent(id));const s=detail.session;$('archive-title').textContent=label(s.mode)+' investigation';$('archive-summary').textContent=`${new Date(s.started_at*1000).toLocaleString()} · ${label(s.outcome||s.status)} · ${detail.evidence_summary.count} retained observations`;
 $('archive-export').href='/api/export?session_id='+encodeURIComponent(id);
 const warning=detail.missing_citations.length?`${detail.missing_citations.length} cited observations have expired. They cannot be verified from this archive.`:detail.evidence_summary.count===0?'Detailed observations have expired or none were recorded. The summary remains available.':'';$('archive-warning').textContent=warning;$('archive-warning').hidden=!warning;
 $('archive-notes').innerHTML=(s.notes||[]).map(n=>`<div class="saved-note"><strong>Investigator note</strong><p>${esc(n.explanation)}</p><small>Uncertainty: ${esc(n.uncertainty||'Not recorded')}</small><div>Citations: ${archiveCitations(n.evidence_ids)}</div></div>`).join('')+
 (s.help||[]).map(h=>`<div class="saved-note"><strong>${esc(AegisView.helpLabel(h.level))}</strong><small>${new Date(h.at*1000).toLocaleString()}</small><p>${esc(h.message)}</p><div>Supporting observations: ${archiveCitations(h.evidence_ids)}</div></div>`).join('')+
 (s.recovery_attempts||[]).map(a=>`<div class="saved-note"><strong>${esc(AegisView.remedyLabel(a.verb||'start'))} · ${esc(a.node)} · ${esc(label(a.status))}</strong><small>${new Date(a.at*1000).toLocaleString()}</small><p>${esc(a.detail||'Control result recorded. See investigation verification for measured recovery.')}</p></div>`).join('')+
 (s.recovery_evidence_ids?.length?`<div class="saved-note"><strong>Verified recovery observations</strong><p>Three consecutive healthy cycles, covering both nodes and the business request path.</p>${archiveCitations(s.recovery_evidence_ids)}</div>`:'')+
 (s.reset_evidence_ids?.length?`<div class="saved-note"><strong>Reset baseline observations</strong><p>Healthy checks after reset are recorded separately from targeted recovery.</p>${archiveCitations(s.reset_evidence_ids)}</div>`:'')+
 (s.debrief?`<p class="panel-description">${esc(s.debrief)}</p>`:'');
 $('archive-notes').querySelectorAll('[data-citation]').forEach(button=>button.addEventListener('click',()=>openCitation(button.dataset.citation,button)));
 const timeline=await request('investigations/'+encodeURIComponent(id)+'/timeline');
 if(archiveId===id)AegisReport.render($('archive-report'),detail,timeline);
 await archivePage();
 }catch(e){$('archive-title').textContent='Archive unavailable';$('archive-summary').textContent=e.message;}finally{archiveBusy=false;}
}
async function archivePage(){const page=await request('investigations/'+encodeURIComponent(archiveId)+'/evidence?limit=30'+(archiveCursor?'&before='+encodeURIComponent(archiveCursor):''));$('archive-evidence').insertAdjacentHTML('beforeend',page.items.map(archiveRecord).join(''));archiveCursor=page.next_cursor;$('archive-more').disabled=!archiveCursor;}
$('archive-more').addEventListener('click',async()=>{if(archiveBusy)return;archiveBusy=true;$('archive-more').disabled=true;try{await archivePage();}catch(e){toast(e.message,true);}finally{archiveBusy=false;}});
// Poll every two seconds; back off while the controller is unreachable and slow down in a hidden tab.
let pollTimer;
function schedule(ms){clearTimeout(pollTimer);pollTimer=setTimeout(poll,ms);}
async function poll(){await refresh();schedule(!connected?retryDelay():document.hidden?10000:2000);}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)schedule(0);});
poll();setInterval(renderTimer,1000);
// A short burst when recovery is verified; a plain message when motion is reduced.
function celebrate(){
 toast('Recovery verified: three healthy, fast cycles in a row.','ok');
 if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
 const host=$('celebrate'),colours=['#1a73e8','#d93025','#fbbc04','#1e8e3e','#9334e6','#12b5cb','#e8710a'];
 const banner=document.createElement('div');banner.className='celebrate-banner';banner.innerHTML='<span>✓</span><strong>Recovery verified</strong><small>Three healthy, fast cycles in a row</small>';
 host.replaceChildren(banner);
 for(let i=0;i<64;i++){const bit=document.createElement('i');const angle=Math.random()*Math.PI*2,reach=140+Math.random()*260;
  bit.style.setProperty('--dx',`${Math.cos(angle)*reach}px`);bit.style.setProperty('--dy',`${Math.sin(angle)*reach*0.7+120}px`);
  bit.style.setProperty('--spin',`${Math.round(Math.random()*720-360)}deg`);bit.style.setProperty('--delay',`${Math.random()*120}ms`);
  bit.style.background=colours[i%colours.length];if(i%3===0)bit.classList.add('round');host.append(bit);}
 host.hidden=false;clearTimeout(celebrate.timer);celebrate.timer=setTimeout(()=>{host.hidden=true;host.replaceChildren();},2600);
}
// Keyboard shortcuts for reading and checking. Nothing that changes the fleet has a shortcut.
$('keys-open').addEventListener('click',()=>$('keys-dialog').showModal());
$('keys-close').addEventListener('click',()=>$('keys-dialog').close());
addEventListener('keydown',e=>{
 if(e.defaultPrevented||e.ctrlKey||e.metaKey||e.altKey||e.target.closest?.('input,textarea,select,[contenteditable]'))return;
 if(document.body.classList.contains('touring')||document.querySelector('dialog[open]'))return;
 const key=e.key.toLowerCase(), press=el=>{if(el&&!el.disabled)el.click();};
 if(key==='?')$('keys-dialog').showModal();
 else if(['a','b','c'].includes(key))press($({a:'node-gateway-a',b:'node-catalog-b',c:'node-gateway-c'}[key]));
 else if(/^[1-9]$/.test(key)&&!e.shiftKey)press(document.querySelectorAll('[data-check]')[Number(key)-1]);
 else if(key==='e')press(document.querySelector('[data-help="evidence"]'));
 else if(key==='p')press($('evidence-live'));
 else if(key==='t')press($('tour-start'));
 else return;
 e.preventDefault();
});
// Highlight the navigation link for the section currently in view.
const navTargets=[...document.querySelectorAll('.nav-links a[href^="#"]')].map(a=>({a,el:document.querySelector(a.getAttribute('href'))?.closest('.panel,.history-grid')})).filter(t=>t.el);
function markNav(current){for(const t of navTargets){t.a.classList.toggle('nav-active',t===current);if(t===current)t.a.setAttribute('aria-current','location');else t.a.removeAttribute('aria-current');}}
// The section filling most of the viewport wins; short final sections still win once they dominate the screen.
function spy(){
 if(performance.now()<(spy.lock||0))return;
 const navBottom=document.querySelector('.topnav').getBoundingClientRect().bottom;
 let current=navTargets[0], best=0;
 for(const t of navTargets){const r=t.el.getBoundingClientRect();const visible=Math.min(r.bottom,innerHeight)-Math.max(r.top,navBottom);if(visible>best){best=visible;current=t;}}
 markNav(current);
}
// A clicked link stays highlighted even if the page cannot scroll its section to the top.
for(const t of navTargets)t.a.addEventListener('click',()=>{spy.lock=performance.now()+900;markNav(t);});
for(const type of ['wheel','touchstart','keydown'])addEventListener(type,()=>{spy.lock=0;},{passive:true});
addEventListener('scroll',()=>{if(spy.queued)return;spy.queued=true;requestAnimationFrame(()=>{spy.queued=false;spy();});},{passive:true});
addEventListener('resize',spy);spy();
