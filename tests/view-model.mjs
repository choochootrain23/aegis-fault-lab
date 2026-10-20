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
  return {passed,scope:'Pure UI state and timer decisions; frontend JavaScript parsed. No visual/browser validation.'};
}
if(typeof process!=='undefined' && process.argv[1] && import.meta.url===new URL('file://'+process.argv[1]).href) console.log(run());
