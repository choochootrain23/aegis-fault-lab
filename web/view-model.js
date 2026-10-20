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
  helpLabel(level) {
    return ({evidence:'Evidence explanation',hint:'Diagnostic hint',action:'Suggested action'})[level]||'Assistance';
  }
});
