'use strict';
// Pure UI decisions: testable without a browser or generated fleet data.
globalThis.AegisView = Object.freeze({
  latestRequest() {
    let sequence=0;
    return {begin:()=>++sequence, current:id=>id===sequence};
  },
  evidenceResult(record) {
    const body=record.body||{};
    if(!body.ok)return {text:body.http_status===507?'Check failed · 507 insufficient storage':'Check failed',tone:'red'};
    if(body.slow)return {text:`Succeeded slowly · ${body.duration_ms} ms`,tone:'amber'};
    if(record.kind==='resources'){
      if(body.cpu_pct_of_limit==null)return {text:`Resources · CPU limit ${body.cpu_limit} · no usage sample`,tone:'amber'};
      return {text:`CPU ${body.cpu_pct_of_limit}% of ${body.cpu_limit}-CPU limit · ${body.throttled_periods??0} throttled periods`,tone:body.cpu_pct_of_limit>=80?'red':body.cpu_pct_of_limit>=50?'amber':'green'};
    }
    if(record.kind==='disk'){
      const disk=body.result||{};
      return {text:`Disk ${disk.used_pct}% used · ${this.bytes(disk.free_bytes)} free`,tone:disk.free_bytes<65536?'red':disk.used_pct>=80?'amber':'green'};
    }
    if(record.kind==='config'){
      const config=body.result||{}, drifted=config.drifted||[];
      if(!Array.isArray(config.settings))return {text:'Config read · detail unavailable',tone:'amber'};
      return drifted.length?{text:`Config drifted · ${drifted.join(', ')} differs from deployed`,tone:'red'}:{text:`Config matches deployed · ${config.settings.length} settings`,tone:'green'};
    }
    if(record.kind==='network'){
      const queues=body.result?.qdiscs||[], netem=queues.find(q=>q.kind==='netem'), resent=body.result?.tcp?.retrans_pct;
      const shaped=netem?` · netem queue${netem.delay_ms?` +${netem.delay_ms} ms`:''}${netem.loss_pct?` dropping ${netem.loss_pct}%`:''}`:'';
      const retrans=resent>=1?` · ${resent}% resent`:'';
      return {text:`Network · connect ${body.median_connect_ms} ms${shaped}${retrans}`,tone:netem||body.median_connect_ms>100||resent>=10?'red':body.median_connect_ms>20||resent>=1?'amber':'green'};
    }
    if(record.kind==='releases'){
      const r=body.result||{}, history=r.history||[], changed=history.length>1&&r.running!==history[0].version;
      return {text:changed?`Release ${r.running} · deployed by ${history[history.length-1].by} after ${history[0].version}`:`Release ${r.running} · unchanged since start`,tone:changed?'amber':'green'};
    }
    if(record.kind==='memory'){
      const m=body.result||{};
      if(body.running===false)return {text:`Memory · container ${body.status}${body.oom_killed?' · killed for running out of memory':''}`,tone:'red'};
      const growth=m.growth_mib_s??0;
      return {text:`Memory ${m.current_mib} of ${m.limit_mib} MiB${growth>=1?` · growing ${growth} MiB/s`:''}${body.oom_killed?' · last exit an OOM kill':''}`,tone:growth>=4||body.oom_killed?'red':m.used_pct>=80||growth>=1?'amber':'green'};
    }
    if(record.kind==='traffic'){
      const t=body.result||{}, callers=t.clients||[], rejected=callers.reduce((a,c)=>a+(c.rejected||0),0), zero=(t.quotas||[]).filter(q=>q.limit_rps===0);
      const total=callers.reduce((a,c)=>a+(c.requests||0),0);
      const retries=callers.reduce((a,c)=>a+(c.retries||0),0), share=total?Math.round(100*retries/total):0;
      return {text:`Traffic · ${total} requests in ${t.window_s} s${rejected?` · ${rejected} rejected`:''}${share>=10?` · ${share}% retries`:''}${zero.length?` · quota 0/s for ${zero.map(q=>q.client).join(', ')}`:''}`,tone:zero.length||rejected||share>=50?'red':share>=10?'amber':'green'};
    }
    if(record.kind==='trace'){
      const trace=body.result||{}, slowest=this.slowestSpan(trace);
      if(trace.request_ok===false)return {text:`Trace · failed at ${trace.failed_at||'unknown step'}`,tone:'red'};
      const where=slowest&&slowest.ms>=(body.outside_handler_ms||0)?`${slowest.name} ${slowest.ms} ms`:`outside handler ${body.outside_handler_ms} ms`;
      return {text:`Trace · ${body.duration_ms} ms · slowest: ${where}`,tone:body.duration_ms>300?'amber':'green'};
    }
    if(record.kind==='service'){
      if(body.running===false)return {text:'Inspection complete · stopped',tone:'red'};
      if(body.running===true)return {text:'Inspection complete · running',tone:'green'};
      return {text:'Inspection complete · state unknown',tone:'amber'};
    }
    if(record.kind==='logs'){
      const entries=body.result?.entries;
      if(!Array.isArray(entries))return {text:'Logs fetched · detail unavailable',tone:'amber'};
      const failed=entries.filter(entry=>entry.ok===false).length, events=entries.filter(entry=>entry.event).length;
      return {text:`Logs fetched · ${failed} failed / ${entries.length} retained entries${events?` · ${events} job events`:''}`,tone:failed||events?'amber':'green'};
    }
    return {text:'Check succeeded',tone:'green'};
  },
  slowestSpan(trace) {
    return (trace?.spans||[]).reduce((a,b)=>(!a||b.ms>a.ms?b:a),null);
  },
  bytes(value) {
    if(value==null)return 'unknown';
    return value>=1048576?`${(value/1048576).toFixed(1)} MiB`:value>=1024?`${Math.round(value/1024)} KiB`:`${value} B`;
  },
  remedyLabel(verb) {
    return ({start:'Start service',calm:'Stop runaway job',clear:'Clear temp files',restore:'Restore config',undelay:'Remove traffic delay',undrop:'Remove packet loss',requota:'Reset client quotas',rollback:'Roll back release',ratelimit:'Rate-limit top client',shed:'Enable load shedding'})[verb]||verb;
  },
  faultLabel(kind) {
    return ({'service-unavailable':'Stopped service','cpu-starvation':'CPU starvation','disk-full':'Disk full','config-drift':'Config drift','network-delay':'Network delay','packet-loss':'Packet loss','quota-zero':'Quota set to zero','bad-release':'Bad release','memory-leak':'Memory leak','traffic-flood':'Traffic flood','retry-storm':'Retry storm'})[kind]||kind;
  },
  // The services a fault can target, when it cannot target all three.
  faultTargets(kind) {
    return ({'quota-zero':['catalog-b'],'traffic-flood':['catalog-b'],'retry-storm':['gateway-a','gateway-c']})[kind]||null;
  },
  // Scenario heading: the selected fault and target, the running practice scenario, or a hidden challenge.
  scenarioCopy(kind, target, mode) {
    if (mode==='challenge') return {number:'??', eyebrow:'Challenge · hidden fault', title:['A ','hidden',' fault'],
      text:'One of eleven faults on one of the three services, picked at random and kept secret. Find it from the evidence, fix it, then reveal it to check.'};
    const kinds=['service-unavailable','cpu-starvation','disk-full','config-drift','network-delay','packet-loss','quota-zero','bad-release','memory-leak','traffic-flood','retry-storm'];
    const copy={
      'service-unavailable':{title:['When a service ','stops',''], text:'Its container is stopped, so its own checks fail.'},
      'cpu-starvation':{title:['When a service is ','starved',' of CPU'], text:'A runaway job uses its whole CPU limit. It still answers, just slowly.'},
      'disk-full':{title:['When the disk ','fills up',''], text:'Its 8 MiB /tmp is full. The process stays live, but every write fails with HTTP 507.'},
      'config-drift':{title:['When config ','drifts',''], text:'A bad reload changes one setting. Liveness, CPU and disk look normal, yet requests fail.'},
      'network-delay':{title:['When the network ','lags',''], text:'Every packet leaving it waits 600 ms. It stays up with idle CPU, yet everything it sends arrives late.'},
      'packet-loss':{title:['When packets ','go missing',''], text:'Half the packets it sends are dropped. Requests sometimes work, sometimes stall, sometimes time out.'},
      'quota-zero':{title:['When the quota ','hits zero',''], text:'A quota push lets the gateways make zero requests. The catalogue is up, fast and green to its own probes.'},
      'bad-release':{title:['When a release ','ships a bug',''], text:'A new version fails about half its requests. The machine is fine and the config unchanged; only the code moved.'},
      'retry-storm':{title:['When retries ','storm',''], text:'App users retry every failure at once. A short slowdown in the catalogue becomes an overload of this gateway that outlives its cause.'},
      'traffic-flood':{title:['When one client ','floods',' the catalogue'], text:'A report export job sends hundreds of requests a second. Nothing is broken; there is simply too much traffic for one small service.'},
      'memory-leak':{title:['When memory ','leaks',''], text:'A new version never frees what it loads. It runs fine until the kernel kills it, then the rollout restarts it, again and again.'},
    }[kind]||{title:['A ','fault',''], text:''};
    const scope=kind==='quota-zero'?'Enforced by the shared catalogue: compare what its callers see with what its own checks say.'
      :kind==='traffic-flood'?'Aimed at the shared catalogue: both gateways feel it. Find out who is calling.'
      :target==='catalog-b'?'On the shared catalogue: see whether both gateways are affected too.':`On ${target}, a local fault: see whether the catalogue and the other gateway stay healthy.`;
    return {number:String(kinds.indexOf(kind)+1).padStart(2,'0'), eyebrow:`Scenario ${String(kinds.indexOf(kind)+1).padStart(2,'0')} · ${this.faultLabel(kind)} · ${target}`,
      title:copy.title, text:`${copy.text} ${scope}`};
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
    const body=node.business.body;
    return `${!body.ok?(body.http_status===507?'Business check failed · 507':'Business check failed'):body.slow?'Business response slow':'Business response'} · ${body.duration_ms} ms`;
  },
  // Suggested next step from public state only; never names a hidden challenge target.
  journey(state, connected) {
    if (!connected || !state) return {step:-1, tone:'alert', title:'Reconnecting to the controller', detail:'Measurements may be stale. Actions stay disabled until polling recovers.', meter:null};
    const s=state.session, streak=Math.min(state.healthy_streak??0,3);
    if (!s || s.status==='ended') {
      if (state.collector_error) return {step:0, tone:'alert', title:'Collector interrupted', detail:state.collector_error, meter:null};
      if (state.baseline_cycles<5) return {step:0, tone:'wait', title:'Collecting a healthy baseline', detail:'All three services must pass five consecutive check cycles before a fault can be injected.', meter:{done:Math.max(0,state.baseline_cycles), total:5}};
      return {step:1, tone:'go', title:s?'Ready for another investigation':'Ready when you are', detail:'Choose practice (known fault) or challenge (hidden fault), then press Start investigation.', meter:null};
    }
    if (s.status==='observing') return {step:2, tone:'wait', title:'Fault injected. Watch the topology', detail:'Checks run every two seconds. Repeated failures open the investigation.', meter:null};
    if (s.status==='investigating') return {step:2, tone:'focus', title:'Find the boundary of the failure', detail:'Select a node and run checks in any order. Compare liveness, latency and business requests, then inspect container state, resources, disk and running config. A trace shows where a request spends its time. When the evidence points to a cause, try the matching recovery action.', meter:null};
    const need=s.required_streak||3, flapping=need>3?' Failures have come and gone, so a short healthy run could be luck.':'';
    const run=Math.min(state.healthy_streak??0,need);
    if (s.status==='verifying' && /has not restored/.test(s.verification||'')) return {step:4, tone:'alert', title:'Not restored yet', detail:'Affected checks still fail or stay slow. Re-check both nodes; a different action or node may be needed.'+flapping, meter:{done:run, total:need}};
    if (s.status==='verifying') return {step:4, tone:'wait', title:'Verifying recovery', detail:`An accepted action is not proof. ${need===3?'Three':need} consecutive healthy, fast cycles are required.${flapping}`, meter:{done:run, total:need}};
    if (s.status==='recovered') return {step:5, tone:'done', title:'Recovery verified', detail:'Record what the evidence supports in your diagnosis, then End & reset to close the investigation.', meter:null};
    return {step:5, tone:'wait', title:'Restoring the baseline', detail:'All three services are started and re-verified. History is preserved.', meter:{done:streak, total:3}};
  },
  checkSummary(record) {
    if (!record) return {text:'Not checked yet', tone:'idle'};
    const result=this.evidenceResult(record), body=record.body||{};
    let outcome=result.tone==='red'?'failed':'ok';
    if(body.ok&&record.kind==='service')outcome=body.running?'running':body.running===false?'stopped':'unknown';
    else if(body.ok&&record.kind==='resources')outcome=body.cpu_pct_of_limit==null?'no sample':`CPU ${body.cpu_pct_of_limit}%`;
    else if(body.ok&&record.kind==='disk')outcome=`${body.result?.used_pct}% used`;
    else if(body.ok&&record.kind==='config')outcome=body.result?.drifted?.length?'drifted':'matches';
    else if(body.ok&&record.kind==='network')outcome=`connect ${body.median_connect_ms} ms${body.result?.tcp?.retrans_pct>=1?` · ${body.result.tcp.retrans_pct}% resent`:''}`;
    else if(body.ok&&record.kind==='releases')outcome=body.result?.running||'read';
    else if(body.ok&&record.kind==='memory')outcome=body.running===false?(body.oom_killed?'OOM killed':'stopped'):`${body.result?.current_mib} MiB${(body.result?.growth_mib_s??0)>=1?' ↑':''}`;
    else if(body.ok&&record.kind==='traffic'){const rejected=(body.result?.clients||[]).reduce((a,c)=>a+(c.rejected||0),0);outcome=rejected?`${rejected} rejected`:'no rejections';}
    else if(body.ok&&record.kind==='trace')outcome=body.result?.request_ok===false?'failed':'traced';
    else if(body.slow)outcome='slow';
    return {text:`${result.tone==='green'?'✓':result.tone==='red'?'✕':'•'} ${outcome}${body.duration_ms!=null?' · '+body.duration_ms+' ms':''}`, tone:result.tone};
  },
  helpLabel(level) {
    return ({evidence:'Evidence explanation',hint:'Diagnostic hint',action:'Suggested action',ai:'AI investigator'})[level]||'Assistance';
  }
});
