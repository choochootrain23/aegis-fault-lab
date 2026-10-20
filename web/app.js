'use strict';
const $ = id => document.getElementById(id);
const refreshOrder = AegisView.latestRequest();
const citationOrder = AegisView.latestRequest();
let historySignature='';
let state, selected = 'gateway-a', busy = false, token = '', chosen = new Set(), evidenceSignature = '', lastSession, connected=false, serverOffset=0, evidencePaused=false, evidenceSnapshot=[], archiveId=null, archiveCursor=null, archiveBusy=false, suggestedCheck=null;
const esc = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label = text => String(text || '').replaceAll('_', ' ').replaceAll('-', ' ');
function setText(id,text){if($(id).textContent!==text)$(id).textContent=text;}
function toast(text, error=false) { const el=$('toast'); el.textContent=text; el.classList.toggle('error',error); el.hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.hidden=true,6000); }
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
  try { const result=await request(path,{...data,...(['recover','reset','extend','reveal','note','help'].includes(path)?{session_id:state.session.id}:{}),...(['sessions','recover','reset','extend','reveal'].includes(path)?{key:crypto.randomUUID()}: {})}); toast(result.status==='unknown'?'Outcome uncertain. Inspect actual state before retrying.':message,result.status==='unknown'); await refresh(); return result; }
  catch(e){toast(e.message,true);}finally{busy=false;document.querySelector('main').setAttribute('aria-busy','false');renderControls();}
}
function renderControls(){
 if(!state)return; const s=state.session, active=s&&s.status!=='ended';
 $('start').disabled=!AegisView.canStart(state,connected,busy);
 $('mode').disabled=busy||active;
 $('practice-target').disabled=busy||active; $('practice-target').hidden=$('mode').value==='challenge';
 $('expiry-continue').disabled=busy||!connected; $('expiry-reset').disabled=busy||!connected;
 $('maintenance').disabled=busy||!connected;
 for(const id of ['reset','save-note','reveal'])$(id).disabled=busy||!connected||!active;
 $('recover').disabled=!AegisView.canRecover(s,connected,busy);
 $('extend').disabled=busy||!connected||!active||['resetting','recovered'].includes(s.status);
 document.querySelectorAll('[data-check]').forEach(b=>b.disabled=busy||!connected);
 document.querySelectorAll('[data-help]').forEach(b=>b.disabled=busy||!connected||!active);
 $('clear-selection').disabled=chosen.size===0;
 $('suggested-check').disabled=busy||!connected;
 $('reveal').hidden=!s||s.mode!=='challenge'||s.revealed||s.status==='ended';
}
function renderEvidence(force=false){
 if(evidencePaused&&!force){const unseen=state.evidence.filter(e=>!evidenceSnapshot.some(old=>old.id===e.id)).length;$('evidence-status').textContent=`Reading paused · ${unseen} newer observations available`;return;}
 if(!evidencePaused)evidenceSnapshot=state.evidence;
 const evidence=evidenceSnapshot.filter(e=>$('filter').value==='all'||e.node===$('filter').value);
 const sig=evidence.map(e=>e.id).join(','); if(sig===evidenceSignature)return; evidenceSignature=sig;
 const open=new Set([...document.querySelectorAll('.evidence-row details[open]')].map(e=>e.dataset.id));
 $('evidence-count').textContent=state.evidence.length;
 $('evidence').innerHTML=evidence.length?evidence.map(e=>`<div class="evidence-row"><input type="checkbox" aria-label="Select evidence ${esc(e.id)}" data-evidence="${esc(e.id)}" ${chosen.has(e.id)?'checked':''}><details data-id="${esc(e.id)}" ${open.has(e.id)?'open':''}><summary><i class="dot ${AegisView.evidenceResult(e).tone}"></i><strong>${esc(e.node)}</strong><span class="kind">${esc(label(e.kind))} · ${e.body.origin==='operator'?'your check':'collector'} · ${esc(AegisView.evidenceResult(e).text)}</span><time>${new Date(e.at*1000).toLocaleTimeString()}</time></summary><p class="evidence-id">Evidence ${esc(e.id)}</p><pre>${esc(JSON.stringify(e.body,null,2))}</pre></details></div>`).join(''):'<div class="empty">No matching evidence yet.<br><small>Choose a diagnostic check to gather observations.</small></div>';
 document.querySelectorAll('[data-evidence]').forEach(el=>el.addEventListener('change',()=>{setEvidencePaused(true); if(el.checked&&chosen.size>=20){el.checked=false;toast('Select up to 20 evidence records for one diagnosis.',true);return;}el.checked?chosen.add(el.dataset.evidence):chosen.delete(el.dataset.evidence);$('selected-count').textContent=chosen.size+' evidence selected';$('clear-selection').disabled=chosen.size===0;}));
}
function setEvidencePaused(paused){evidencePaused=paused;$('evidence-live').textContent=paused?'Resume live updates':'Pause updates';$('evidence-live').setAttribute('aria-pressed',String(!paused));$('evidence-status').textContent=paused?'Reading paused · your evidence stays in place':'Following new observations';if(!paused){evidenceSignature='';renderEvidence();}}
function render(){
 const s=state.session;
 if(lastSession!==s?.id){chosen.clear();lastSession=s?.id;evidencePaused=false;evidenceSignature='';setEvidencePaused(false);$('help-result').hidden=true;$('suggested-check').hidden=true;suggestedCheck=null;$('selected-count').textContent='0 evidence selected';}
 const unknown=state.nodes.every(n=>n.health==='unknown');
 const active=s&&s.status!=='ended';
 const baselineFailed=!active&&state.nodes.some(n=>n.health!=='healthy');
 $('notice').classList.toggle('error',!!state.collector_error||unknown||baselineFailed);
 setText('notice',state.collector_error|| (unknown?'Fleet measurements unavailable. Start the project containers; unknown is not healthy.':baselineFailed?'Baseline checks are failing. Start the project containers and inspect the failed observations before beginning.':active?'Investigation active. Checks reflect real service responses; choose your own path.':s?'Investigation ended. Review its evidence or start a new scenario once the baseline is healthy.':'Baseline collection is live. Five healthy cycles are required before a controlled fault.'));
 $('baseline').textContent=`${Math.min(state.baseline_cycles,5)}/5 healthy baseline cycles`;
 $('session-status').textContent=s?label(s.status):'Baseline collection';
 $('session-dot').className='dot '+(s?.status==='recovered'?'green':s&&s.status!=='ended'?'amber':'');
 $('verification').textContent=s?.verification||'A successful control action is followed by three healthy verification cycles.';
 for(const n of state.nodes){const el=$('node-'+n.id); const badge=el.querySelector('.node-state');badge.textContent=label(n.health);badge.className='node-state '+n.health;
 el.querySelector('.node-measure').textContent=AegisView.measurement(n);}
 const gateway=state.nodes.find(n=>n.id==='gateway-a');const measured=gateway?.business;
 $('edge').classList.toggle('failed',!!measured&&!measured.body.ok);
 $('edge-label').textContent=measured?(gateway.health==='unknown'?'Previous observation is stale':measured.body.ok?'Observed business request succeeded':'Observed business request failed'):'Waiting for observed requests';
 $('scenario-reveal').textContent=s?.scenario?`Known scenario: ${label(s.scenario.kind)} on ${s.scenario.target}`:'';
 $('debrief').textContent=s?.debrief||'';
 const attempts=(s?.recovery_attempts||[]).map(a=>({at:a.at,title:`Start ${a.node}`,detail:a.status==='succeeded'?'Control action accepted; consult verification':a.detail||a.status}));
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
 renderEvidence();renderControls();renderTimer();
}
function renderTimer(){
 const timer=AegisView.timer(state?.session,Date.now()/1000+serverOffset),dialog=$('expiry-dialog');
 if(!timer.running){$('timer').textContent='No active timer';$('timer').classList.remove('timer-warning');if(dialog.open)dialog.close();return;}
 $('timer').classList.toggle('timer-warning',timer.warning||timer.expired);
 $('timer').textContent=timer.expired?'Time expired':`${Math.floor(timer.remaining/60)}:${String(timer.remaining%60).padStart(2,'0')} remaining${timer.warning?' · extend soon':''}`;
 if(timer.expired){setText('grace-text',`Automatic reset in ${timer.grace} seconds if you do not continue.`);if(!dialog.open&&!$('archive-dialog').open)dialog.showModal();}else if(dialog.open)dialog.close();
}
async function refresh(){const sequence=refreshOrder.begin();try{const next=await request('state');if(!refreshOrder.current(sequence))return;state=next;connected=true;serverOffset=state.now-Date.now()/1000;token=state.token;render();}catch(e){if(!refreshOrder.current(sequence))return;connected=false;renderControls();$('notice').classList.add('error');$('notice').textContent='Controller unavailable. Displayed measurements may be stale. '+e.message;document.querySelectorAll('.node-state').forEach(e=>{e.textContent='Unknown';e.className='node-state unknown';});}}
document.querySelectorAll('[data-node]').forEach(b=>b.addEventListener('click',()=>{selected=b.dataset.node;document.querySelectorAll('[data-node]').forEach(n=>{n.classList.toggle('selected',n===b);n.setAttribute('aria-pressed',String(n===b));});$('inspector-title').textContent=selected;}));
document.querySelectorAll('[data-check]').forEach(b=>b.addEventListener('click',()=>action('check',{node:selected,kind:b.dataset.check},'Fresh observation added to the evidence notebook')));
$('start').addEventListener('click',()=>action('sessions',{mode:$('mode').value,target:$('practice-target').value},'Investigation started. Observe the measured effects.'));
$('recover').addEventListener('click',()=>action('recover',{node:selected},'Start requested. Watch the verification checks.'));
$('reset').addEventListener('click',()=>action('reset',{},'Reset requested. History is preserved.'));
$('extend').addEventListener('click',()=>action('extend',{},'Investigation extended by 15 minutes'));
$('expiry-continue').addEventListener('click',()=>action('extend',{},'Continue exploring for 15 minutes'));
$('expiry-reset').addEventListener('click',()=>action('reset',{},'Reset requested. History is preserved.'));
$('reveal').addEventListener('click',()=>action('reveal',{},'Fault revealed. This is no longer a blind attempt.'));
$('filter').addEventListener('change',()=>{evidenceSignature='';renderEvidence(true);});
document.querySelectorAll('[data-help]').forEach(b=>b.addEventListener('click',async()=>{
 const result=await action('help',{level:b.dataset.help},'Assistance recorded');
 if(!result)return;
 $('help-result').textContent=result.message;$('help-result').hidden=false;
 suggestedCheck=result.suggested_check;$('suggested-check').hidden=!suggestedCheck;
 if(suggestedCheck)$('suggested-check').textContent=`Run ${label(suggestedCheck.kind)} · ${suggestedCheck.node} ↗`;
 if(result.evidence_ids?.length){
   // Cited service checks may have left the recent polling window. Keep their
   // exact retained records visible instead of silently missing the highlight.
   const records=new Map([...(result.evidence||[]),...state.evidence].map(e=>[e.id,e]));
   evidenceSnapshot=[...records.values()].sort((a,b)=>b.at-a.at);
   $('filter').value='all';evidenceSignature='';setEvidencePaused(true);renderEvidence(true);
   for(const id of result.evidence_ids){const detail=document.querySelector(`details[data-id="${CSS.escape(id)}"]`);if(detail){detail.open=true;detail.closest('.evidence-row').classList.add('highlight');}}
 }
}));
$('suggested-check').addEventListener('click',()=>{if(suggestedCheck)action('check',suggestedCheck,'Suggested check completed. Review the new observation.');});
$('clear-selection').addEventListener('click',()=>{chosen.clear();$('selected-count').textContent='0 evidence selected';$('clear-selection').disabled=true;evidenceSignature='';renderEvidence(true);});

$('note-form').addEventListener('submit',async e=>{e.preventDefault();const saved=await action('note',{explanation:$('explanation').value,uncertainty:$('uncertainty').value,evidence_ids:[...chosen]},'Diagnosis saved with evidence references');if(saved){$('explanation').value='';$('uncertainty').value='';chosen.clear();$('selected-count').textContent='0 evidence selected';evidenceSignature='';renderEvidence(true);}});
$('expiry-dialog').addEventListener('cancel',e=>e.preventDefault());
$('mode').addEventListener('change',renderControls);
$('evidence-live').addEventListener('click',()=>setEvidencePaused(!evidencePaused));
$('evidence').addEventListener('toggle',e=>{if(e.target.tagName==='DETAILS'&&e.target.open)setEvidencePaused(true);},true);
$('maintenance').addEventListener('click',()=>action('maintenance',{},'Retention cleanup complete. Active investigation evidence is preserved.'));
$('archive-close').addEventListener('click',()=>$('archive-dialog').close());
function archiveRecord(e){return `<details class="archive-record"><summary><strong>${esc(e.node)}</strong> · ${esc(label(e.kind))} · ${esc(AegisView.evidenceResult(e).text)} <time>${new Date(e.at*1000).toLocaleString()}</time></summary><p class="evidence-id">${esc(e.id)}</p><pre>${esc(JSON.stringify(e.body,null,2))}</pre></details>`;}
function archiveCitations(ids){return ids.length?ids.map(id=>`<button class="button compact citation" data-citation="${esc(id)}" title="Open this exact observation">↗ ${esc(id.slice(0,8))}</button>`).join(' '):'None recorded';}
async function openCitation(id,button){
 const session=archiveId, sequence=citationOrder.begin();button.disabled=true;
 try{const item=await request('investigations/'+encodeURIComponent(session)+'/evidence/'+encodeURIComponent(id));if(archiveId!==session||!citationOrder.current(sequence))return;
 $('archive-citation').innerHTML=archiveRecord(item);$('archive-citation').hidden=false;
 const detail=$('archive-citation').querySelector('details');detail.open=true;detail.querySelector('summary').focus();$('archive-citation').scrollIntoView({block:'nearest'});
 }catch(e){if(citationOrder.current(sequence))toast(e.message,true);}finally{button.disabled=false;}
}
async function openArchive(id){
 if(archiveBusy)return;archiveBusy=true;archiveId=id;archiveCursor=null;$('archive-citation').replaceChildren();$('archive-citation').hidden=true;$('archive-evidence').replaceChildren();$('archive-notes').replaceChildren();$('archive-warning').hidden=true;$('archive-more').disabled=true;$('archive-title').textContent='Loading investigation…';$('archive-summary').textContent='';$('archive-export').removeAttribute('href');if(!$('archive-dialog').open)$('archive-dialog').showModal();
 try{const detail=await request('investigations/'+encodeURIComponent(id));const s=detail.session;$('archive-title').textContent=label(s.mode)+' investigation';$('archive-summary').textContent=`${new Date(s.started_at*1000).toLocaleString()} · ${label(s.outcome||s.status)} · ${detail.evidence_summary.count} retained observations`;
 $('archive-export').href='/api/export?session_id='+encodeURIComponent(id);
 const warning=detail.missing_citations.length?`${detail.missing_citations.length} cited observations have expired. They cannot be verified from this archive.`:detail.evidence_summary.count===0?'Detailed observations have expired or none were recorded. The summary remains available.':'';$('archive-warning').textContent=warning;$('archive-warning').hidden=!warning;
 $('archive-notes').innerHTML=(s.notes||[]).map(n=>`<div class="saved-note"><strong>Investigator note</strong><p>${esc(n.explanation)}</p><small>Uncertainty: ${esc(n.uncertainty||'Not recorded')}</small><div>Citations: ${archiveCitations(n.evidence_ids)}</div></div>`).join('')+
 (s.help||[]).map(h=>`<div class="saved-note"><strong>${esc(AegisView.helpLabel(h.level))}</strong><small>${new Date(h.at*1000).toLocaleString()}</small><p>${esc(h.message)}</p><div>Supporting observations: ${archiveCitations(h.evidence_ids)}</div></div>`).join('')+
 (s.recovery_attempts||[]).map(a=>`<div class="saved-note"><strong>Start ${esc(a.node)} · ${esc(label(a.status))}</strong><small>${new Date(a.at*1000).toLocaleString()}</small><p>${esc(a.detail||'Control result recorded. See investigation verification for measured recovery.')}</p></div>`).join('')+
 (s.recovery_evidence_ids?.length?`<div class="saved-note"><strong>Verified recovery observations</strong><p>Three consecutive healthy cycles, covering both nodes and the business request path.</p>${archiveCitations(s.recovery_evidence_ids)}</div>`:'')+
 (s.reset_evidence_ids?.length?`<div class="saved-note"><strong>Reset baseline observations</strong><p>Healthy checks after reset are recorded separately from targeted recovery.</p>${archiveCitations(s.reset_evidence_ids)}</div>`:'')+
 (s.debrief?`<p class="panel-description">${esc(s.debrief)}</p>`:'');
 $('archive-notes').querySelectorAll('[data-citation]').forEach(button=>button.addEventListener('click',()=>openCitation(button.dataset.citation,button)));
 await archivePage();
 }catch(e){$('archive-title').textContent='Archive unavailable';$('archive-summary').textContent=e.message;}finally{archiveBusy=false;}
}
async function archivePage(){const page=await request('investigations/'+encodeURIComponent(archiveId)+'/evidence?limit=30'+(archiveCursor?'&before='+encodeURIComponent(archiveCursor):''));$('archive-evidence').insertAdjacentHTML('beforeend',page.items.map(archiveRecord).join(''));archiveCursor=page.next_cursor;$('archive-more').disabled=!archiveCursor;}
$('archive-more').addEventListener('click',async()=>{if(archiveBusy)return;archiveBusy=true;$('archive-more').disabled=true;try{await archivePage();}catch(e){toast(e.message,true);}finally{archiveBusy=false;}});
async function poll(){await refresh();setTimeout(poll,2000);}poll();setInterval(renderTimer,1000);
