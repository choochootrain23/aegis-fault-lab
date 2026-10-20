'use strict';
// Pure UI decisions: testable without a browser or generated fleet data.
globalThis.AegisView = Object.freeze({
  latestRequest() {
    let sequence=0;
    return {begin:()=>++sequence, current:id=>id===sequence};
  },
  evidenceResult(record) {
    const body=record.body||{};
    if(!body.ok)return {text:'Check failed',tone:'red'};
    if(record.kind==='service'){
      if(body.running===false)return {text:'Inspection complete · stopped',tone:'red'};
      if(body.running===true)return {text:'Inspection complete · running',tone:'green'};
      return {text:'Inspection complete · state unknown',tone:'amber'};
    }
    if(record.kind==='logs'){
      const entries=body.result?.entries;
      if(!Array.isArray(entries))return {text:'Logs fetched · detail unavailable',tone:'amber'};
      const failed=entries.filter(entry=>entry.ok===false).length;
      return {text:`Logs fetched · ${failed} failed / ${entries.length} retained requests`,tone:failed?'amber':'green'};
    }
    return {text:'Check succeeded',tone:'green'};
  },
  timer(session, serverNow) {
    if (!session || ['ended', 'recovered', 'resetting'].includes(session.status)) {
      return {running:false, expired:false, remaining:0, grace:0, warning:false};
    }
    const remaining=Math.ceil(session.deadline-serverNow);
    return {running:true, expired:remaining<=0, remaining:Math.max(0,remaining),
      grace:Math.max(0,Math.min(60,60+remaining)), warning:remaining>0&&remaining<=60};
  },
  canRecover(session, connected, busy) {
    return connected && !busy && !!session && !['ended','resetting'].includes(session.status);
  },
  canStart(state, connected, busy) {
    return connected && !busy && !!state && (!state.session || state.session.status==='ended') &&
      state.baseline_cycles>=5 && !state.collector_error && state.nodes.every(n=>n.health==='healthy');
  },
  measurement(node) {
    if (node.health==='unknown') return 'No fresh measurement';
    if (!node.business) return 'Waiting for business check';
    return `${node.business.body.ok?'Business response':'Business check failed'} · ${node.business.body.duration_ms} ms`;
  },
  // Suggested next step from public state only; never names a hidden challenge target.
  journey(state, connected) {
    if (!connected || !state) return {step:-1, tone:'alert', title:'Reconnecting to the controller', detail:'Measurements may be stale. Actions stay disabled until polling recovers.', meter:null};
    const s=state.session, streak=Math.min(state.healthy_streak??0,3);
    if (!s || s.status==='ended') {
      if (state.collector_error) return {step:0, tone:'alert', title:'Collector interrupted', detail:state.collector_error, meter:null};
      if (state.baseline_cycles<5) return {step:0, tone:'wait', title:'Collecting a healthy baseline', detail:'Both services must pass five consecutive check cycles before a fault can be injected.', meter:{done:Math.max(0,state.baseline_cycles), total:5}};
      return {step:1, tone:'go', title:s?'Ready for another investigation':'Ready when you are', detail:'Choose practice (known fault) or challenge (hidden fault), then press Start investigation.', meter:null};
    }
    if (s.status==='observing') return {step:2, tone:'wait', title:'Fault injected. Watch the topology', detail:'Checks run every two seconds. Repeated failures open the investigation.', meter:null};
    if (s.status==='investigating') return {step:2, tone:'focus', title:'Find the boundary of the failure', detail:'Select a node and run checks in any order. Compare liveness with business requests, then inspect container state. When the evidence points to a stopped service, try starting it.', meter:null};
    if (s.status==='verifying' && /has not restored/.test(s.verification||'')) return {step:4, tone:'alert', title:'Not restored yet', detail:'Affected checks still fail. Re-check both nodes; a different service may need starting.', meter:{done:streak, total:3}};
    if (s.status==='verifying') return {step:4, tone:'wait', title:'Verifying recovery', detail:'An accepted start is not proof. Three consecutive healthy cycles are required.', meter:{done:streak, total:3}};
    if (s.status==='recovered') return {step:5, tone:'done', title:'Recovery verified', detail:'Record what the evidence supports in your diagnosis, then End & reset to close the investigation.', meter:null};
    return {step:5, tone:'wait', title:'Restoring the baseline', detail:'Both services are started and re-verified. History is preserved.', meter:{done:streak, total:3}};
  },
  checkSummary(record) {
    if (!record) return {text:'Not run', tone:''};
    const result=this.evidenceResult(record), body=record.body||{};
    const outcome=record.kind==='service'&&body.ok?(body.running?'running':body.running===false?'stopped':'unknown'):result.tone==='red'?'failed':'ok';
    return {text:`${result.tone==='green'?'✓':result.tone==='red'?'✕':'•'} ${outcome}${body.duration_ms!=null?' · '+body.duration_ms+' ms':''}`, tone:result.tone};
  },
  helpLabel(level) {
    return ({evidence:'Evidence explanation',hint:'Diagnostic hint',action:'Suggested action'})[level]||'Assistance';
  }
});
