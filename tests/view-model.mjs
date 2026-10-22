// Run with `node tests/view-model.mjs` or import run() from a Node runtime.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

export function run(){
  const context={};
  new vm.Script(readFileSync(new URL('../web/view-model.js',import.meta.url),'utf8')).runInNewContext(context);
  const view=context.AegisView;
  let passed=0;
  const check=(name,fn)=>{try{fn();passed++;}catch(error){throw new Error(name+': '+error.message);}};
  check('Older polling response cannot replace a newer refresh',()=>{
    const gate=view.latestRequest(), old=gate.begin(), recent=gate.begin();
    assert.equal(gate.current(old),false);
    assert.equal(gate.current(recent),true);
  });
  check('Successful inspection does not label a stopped service healthy',()=>{
    const result=view.evidenceResult({kind:'service',body:{ok:true,running:false}});
    assert.equal(result.tone,'red');assert.match(result.text,/stopped/);
  });
  check('Missing lifecycle detail is explicit',()=>{
    assert.equal(view.evidenceResult({kind:'service',body:{ok:true}}).tone,'amber');
  });
  check('Failed inspection does not claim a stopped service',()=>{
    assert.equal(view.evidenceResult({kind:'service',body:{ok:false,running:false}}).text,'Check failed');
  });
  check('Fetched logs summarize retained failures without claiming current outage',()=>{
    const result=view.evidenceResult({kind:'logs',body:{ok:true,result:{entries:[{ok:true},{ok:false},{ok:false}]}}});
    assert.equal(result.tone,'amber');assert.equal(result.text,'Logs fetched · 2 failed / 3 retained entries');
  });
  check('No timer without a session',()=>assert.equal(view.timer(null,100).running,false));
  check('Healthy session has 15 minute timer',()=>assert.equal(view.timer({status:'investigating',deadline:1000},100).remaining,900));
  check('Warn before expiry',()=>assert.equal(view.timer({status:'investigating',deadline:1000},950).warning,true));
  check('Expiry starts 60 second grace',()=>assert.equal(view.timer({status:'investigating',deadline:1000},1000).grace,60));
  check('Grace counts down',()=>assert.equal(view.timer({status:'investigating',deadline:1000},1045).grace,15));
  check('Grace never becomes negative',()=>assert.equal(view.timer({status:'investigating',deadline:1000},1200).grace,0));
  check('Reset cancels timer prompts',()=>assert.equal(view.timer({status:'resetting',deadline:1000},1200).running,false));
  check('Recovery controls cannot interrupt a reset',()=>assert.equal(view.canRecover({status:'resetting'},true,false),false));
  check('Disconnected controls cannot mutate',()=>assert.equal(view.canRecover({status:'investigating'},false,false),false));
  check('Busy controls cannot double submit',()=>assert.equal(view.canRecover({status:'investigating'},true,true),false));
  check('Safe recovery available without diagnosis',()=>assert.equal(view.canRecover({status:'investigating',notes:[]},true,false),true));
  const healthy={session:null,baseline_cycles:5,collector_error:null,nodes:[{health:'healthy'},{health:'healthy'}]};
  check('Healthy baseline enables start',()=>assert.equal(view.canStart(healthy,true,false),true));
  check('Stale baseline cannot enable injection',()=>assert.equal(view.canStart({...healthy,nodes:[{health:'unknown'}]},true,false),false));
  check('Collector error blocks start',()=>assert.equal(view.canStart({...healthy,collector_error:'Persistence interrupted'},true,false),false));
  check('An active session blocks another',()=>assert.equal(view.canStart({...healthy,session:{status:'recovered'}},true,false),false));
  check('Stale results are not displayed as fresh success',()=>assert.equal(view.measurement({health:'unknown',business:{body:{ok:true,duration_ms:1}}}),'No fresh measurement'));
  check('Drifted config is a red finding',()=>{
    const result=view.evidenceResult({kind:'config',body:{ok:true,result:{drifted:['upstream'],settings:[{key:'upstream',drifted:true}]}}});
    assert.equal(result.tone,'red');assert.match(result.text,/upstream/);
  });
  check('Failed trace names the failing step',()=>{
    assert.match(view.evidenceResult({kind:'trace',body:{ok:true,duration_ms:9,result:{request_ok:false,failed_at:'catalogue load',spans:[]}}}).text,/catalogue load/);
  });
  check('Slow trace names the slowest step',()=>{
    const result=view.evidenceResult({kind:'trace',body:{ok:true,duration_ms:620,outside_handler_ms:20,result:{request_ok:true,spans:[{name:'receipt write',ms:4},{name:'upstream catalog-b:8000/catalog',ms:590}]}}});
    assert.equal(result.tone,'amber');assert.match(result.text,/upstream catalog-b:8000\/catalog 590 ms/);
  });
  // Parse the actual event wiring too; layout/visual review still requires a browser.
  new vm.Script(readFileSync(new URL('../web/app.js',import.meta.url),'utf8'));
  check('Packet loss and retransmissions are named on the network result',()=>{
    const result=view.evidenceResult({kind:'network',body:{ok:true,median_connect_ms:1.1,result:{qdiscs:[{kind:'netem',loss_pct:40}],tcp:{retrans_pct:24.6}}}});
    assert.equal(result.tone,'red');assert.match(result.text,/dropping 40%/);assert.match(result.text,/24.6% resent/);
    assert.match(view.checkSummary({kind:'network',body:{ok:true,median_connect_ms:1.1,result:{tcp:{retrans_pct:24.6}}}}).text,/24.6% resent/);
  });
  check('A zero quota and rejected callers read as a failure',()=>{
    const traffic={kind:'traffic',body:{ok:true,result:{window_s:60,clients:[{client:'gateway-a',requests:30,rejected:30}],quotas:[{client:'gateway-a',limit_rps:0}]}}};
    const result=view.evidenceResult(traffic);
    assert.equal(result.tone,'red');assert.match(result.text,/30 rejected/);assert.match(result.text,/quota 0\/s for gateway-a/);
    assert.match(view.checkSummary(traffic).text,/30 rejected/);
    assert.equal(view.evidenceResult({kind:'traffic',body:{ok:true,result:{window_s:60,clients:[{client:'controller',requests:5,rejected:0}],quotas:[]}}}).tone,'green');
  });
  check('Quota faults only target the catalogue',()=>{
    assert.deepEqual([...view.faultTargets('quota-zero')],['catalog-b']);assert.equal(view.faultTargets('packet-loss'),null);
  });
  check('Flapping raises the verification meter to ten cycles',()=>{
    const journey=view.journey({session:{status:'verifying',required_streak:10},healthy_streak:4},true);
    assert.deepEqual({...journey.meter},{done:4,total:10});assert.match(journey.detail,/10 consecutive/);assert.match(journey.detail,/could be luck/);
    assert.deepEqual({...view.journey({session:{status:'verifying'},healthy_streak:2},true).meter},{done:2,total:3});
  });
  check('New fixes have readable labels',()=>{
    assert.equal(view.remedyLabel('undrop'),'Remove packet loss');assert.equal(view.remedyLabel('requota'),'Reset client quotas');
    assert.equal(view.scenarioCopy('quota-zero','catalog-b','practice').number,'07');
  });
  check('A release deployed after start is flagged, an unchanged one is not',()=>{
    const changed=view.evidenceResult({kind:'releases',body:{ok:true,result:{running:'v1.5.0',history:[{version:'v1.4.2',by:'deployment'},{version:'v1.5.0',by:'release-bot'}]}}});
    assert.equal(changed.tone,'amber');assert.match(changed.text,/v1.5.0 · deployed by release-bot after v1.4.2/);
    assert.equal(view.evidenceResult({kind:'releases',body:{ok:true,result:{running:'v1.4.2',history:[{version:'v1.4.2',by:'deployment'}]}}}).tone,'green');
  });
  check('Memory growth and an OOM kill read as failures',()=>{
    const growing=view.evidenceResult({kind:'memory',body:{ok:true,running:true,result:{current_mib:130,limit_mib:192,growth_mib_s:12.1}}});
    assert.equal(growing.tone,'red');assert.match(growing.text,/growing 12.1 MiB\/s/);
    const killed=view.evidenceResult({kind:'memory',body:{ok:true,running:false,status:'exited',oom_killed:true}});
    assert.equal(killed.tone,'red');assert.match(killed.text,/killed for running out of memory/);
    assert.match(view.checkSummary({kind:'memory',body:{ok:true,running:false,oom_killed:true}}).text,/OOM killed/);
    assert.equal(view.evidenceResult({kind:'memory',body:{ok:true,running:true,result:{current_mib:45,limit_mib:192,used_pct:23,growth_mib_s:0.1}}}).tone,'green');
  });
  check('Release faults have readable labels',()=>{
    assert.equal(view.remedyLabel('rollback'),'Roll back release');assert.equal(view.scenarioCopy('memory-leak','catalog-b','practice').number,'09');
  });
  check('Traffic flood is catalogue-only and has a readable fix',()=>{
    assert.deepEqual([...view.faultTargets('traffic-flood')],['catalog-b']);assert.equal(view.remedyLabel('ratelimit'),'Rate-limit top client');
    assert.equal(view.scenarioCopy('traffic-flood','catalog-b','practice').number,'10');assert.match(view.scenarioCopy('x','catalog-b','challenge').text,/One of \w+ faults on one of the three services/);
  });
  check('A traffic record that is mostly retries reads as a failure',()=>{
    const storm=view.evidenceResult({kind:'traffic',body:{ok:true,result:{window_s:60,clients:[{client:'mobile-app',requests:9000,retries:6600,rejected:0}],quotas:[]}}});
    assert.equal(storm.tone,'red');assert.match(storm.text,/73% retries/);
    assert.deepEqual([...view.faultTargets('retry-storm')],['gateway-a','gateway-c']);assert.equal(view.remedyLabel('shed'),'Enable load shedding');
  });
  check('Facility records are labelled simulated and an open breaker reads as a failure',()=>{
    const open=view.evidenceResult({kind:'facility',body:{ok:true,simulated:true,pdu:'PDU-A',rack:'R1',breaker:'open'}});
    assert.equal(open.tone,'red');assert.match(open.text,/simulated/);assert.match(open.text,/no power/);
    assert.equal(view.evidenceResult({kind:'facility',body:{ok:true,simulated:true,pdu:'PDU-B',rack:'R2',breaker:'closed',load_kw:0.15,inlet_c:25.1}}).tone,'green');
    assert.equal(view.remedyLabel('breaker'),'Reset breaker');assert.deepEqual([...view.faultTargets('power-loss')],['catalog-b']);
  });
  check('An active power cap reads as a facility failure',()=>{
    const cap=view.evidenceResult({kind:'facility',body:{ok:true,simulated:true,pdu:'PDU-A',breaker:'closed',power_cap:{active:true,cpu_limit:0.05},cooling:{crac_2:'failed'},inlet_c:38.4}});
    assert.equal(cap.tone,'red');assert.match(cap.text,/power cap active/);assert.match(cap.text,/cooling failed/);
    assert.equal(view.remedyLabel('uncap'),'Lift power cap');
  });
  check('A failed name lookup reads as a network failure and the DNS fault is gateway-only',()=>{
    const failing={kind:'network',body:{ok:true,median_connect_ms:0.4,result:{qdiscs:[],dns:{name:'catalog-b',ok:false,changed:true,nameserver:'127.0.0.53',error:'catalog-b: connection refused by nameserver 127.0.0.53'}}}};
    const result=view.evidenceResult(failing);
    assert.equal(result.tone,'red');assert.match(result.text,/cannot resolve catalog-b \(resolver 127.0.0.53\)/);
    assert.match(view.checkSummary(failing).text,/DNS failing/);
    assert.equal(view.evidenceResult({kind:'network',body:{ok:true,median_connect_ms:0.4,result:{qdiscs:[],dns:{name:'catalog-b',ok:true}}}}).tone,'green');
    assert.deepEqual([...view.faultTargets('dns-failure')],['gateway-a','gateway-c']);assert.equal(view.remedyLabel('resolver'),'Restore resolver');
    assert.equal(view.scenarioCopy('dns-failure','gateway-a','practice').number,'14');
  });
  return {passed,scope:'Pure UI state and timer decisions; frontend JavaScript parsed. No visual/browser validation.'};
}
if(typeof process!=='undefined' && process.argv[1] && import.meta.url===new URL('file://'+process.argv[1]).href) console.log(run());
