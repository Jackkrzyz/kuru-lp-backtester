const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(){this.textContent='';this.children=[];this.style={};this.handlers={};this.checked=false;this.value='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];this.replacements=(this.replacements||0)+1;}
  setAttribute(){}
  addEventListener(name,handler){this.handlers[name]=handler;}
}
const elements=new Map();
const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let snapshot={market:'0xmarket',deployment_block:100,scope_to_block:200,total_scope_blocks:101,
  downloaded_ranges:[{from_block:110,to_block:120}],verified_ranges:[],trade_only_ranges:[{from_block:100,to_block:200}],
  downloaded_blocks:11,verified_blocks:0,storage_bytes:{'.cache':2**20},total_saved_bytes:2**20,free_disk_bytes:2**30,
  saved_file_count:2,scope_is_finalized:true,network:{checked_at:1},controls_available:true,token_available:true,jobs:[]};
const requests=[];let failure=null, timer;
const loadedParts=[];
vm.runInNewContext(fs.readFileSync(require.resolve('../frontend/data_suite.js'),'utf8'),{
  window:{LabJobs:{update(value){get('captured-jobs').value=value;}},dispatchEvent(){}},CustomEvent:class {},
  PageLoading:{ready(part){loadedParts.push(part);},error(){}},
  document:{getElementById:get,createElement:()=>new Element()},Number,Date,Math,JSON,AbortSignal,setTimeout(callback){timer=callback;},
  fetch:async(path,options)=>{
    requests.push({path,options});
    return {ok:!failure,json:async()=>failure ? {error:failure} : path==='/api/data-suite' ? snapshot : {job_id:'new'}};
  }
});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();assert.equal(requests.length,1);assert.equal(requests[0].options.method,'GET');
  assert.equal(get('archive-coverage').children.length,4);
  assert.equal(get('archive-coverage').children[0].children[1].children[0].style.left,(10/101*100)+'%');
  assert.equal(get('verified-blocks').textContent,'0');assert.equal(get('saved-size').textContent,'1.00 MiB');
  assert.ok(loadedParts.includes('inventory'));
  assert.equal(get('start-download').disabled,true,'Explicit confirmation is required');
  get('confirm-download').checked=true;get('confirm-download').handlers.change();
  assert.equal(get('start-download').disabled,false);
  get('download-from').value=130;get('download-to').value=140;
  get('download-form').handlers.submit({preventDefault(){}});
  assert.equal(get('start-download').disabled,true,'Disable while submitting');
  get('download-form').handlers.submit({preventDefault(){}});
  await flush();
  const sent=requests.find(request=>request.path==='/api/data-suite/prepare');
  assert.equal(requests.filter(request=>request.path==='/api/data-suite/prepare').length,1,'Prevent duplicate launch');
  assert.deepEqual(JSON.parse(sent.options.body),{market:'0xmarket',from_block:130,to_block:140,confirm_download:true});
  get('download-from').handlers.input();assert.equal(get('confirm-download').checked,false);
  failure='Rate limit test';get('refresh-head').handlers.click();await flush();
  assert.equal(get('suite-error').textContent,'Rate limit test');
  assert.equal(get('suite-error').hidden,false);
  failure=null;
  const rebuilds=get('archive-coverage').replacements;
  timer();await flush();
  assert.equal(get('archive-coverage').replacements,rebuilds);
  assert.equal(get('suite-error').hidden,true,'A recovered status request clears the old request error');
  snapshot={...snapshot,inventory_ready:false,inventory_refreshing:true,total_saved_bytes:null,verified_blocks:null};
  timer();await flush();
  assert.equal(get('saved-size').textContent,'—','Unknown storage is not zero');
  assert.equal(get('verified-blocks').textContent,'—');
  assert.ok(get('inventory-status').textContent.includes('background'));
  assert.equal(get('download-from').value,130,'A background refresh must not reset entered bounds');
  snapshot={...snapshot,token_available:false,jobs:[{job_id:'abc123',request:{from_block:100,to_block:199,partition_blocks:10},
    running:true,progress:{downloaded_events:42},verification_bounds:[100,149]}],verifications:[]};
  timer();await flush();
  assert.equal(get('start-download').disabled,true,'A running legacy job blocks the combined launch');
  snapshot={...snapshot,pipeline:{running:true,phase:'planning',job_id:'pipeline'}};
  timer();await flush();assert.equal(get('stop-download').disabled,false,'Can stop during planning');
  get('stop-download').handlers.click();await flush();
  assert.ok(requests.some(request=>request.path==='/api/data-suite/prepare-stop'));
  snapshot={...snapshot,jobs:[],pipeline:{running:false,phase:'failed',error:'RPC check failed'}};
  timer();await flush();assert.equal(get('start-download').disabled,true,'A token is required for the combined job');
  assert.equal(get('process-saved-data').disabled,false,'Local processing needs no Envio token');
  get('process-saved-data').handlers.click();await flush();
  assert.ok(requests.some(request=>request.path==='/api/data-suite/prepare-local'));
  snapshot={...snapshot,verification_queue:{running:true,phase:'verifying',progress:{}}};
  timer();await flush();assert.equal(get('process-saved-data').disabled,true,'Do not start two verification queues');
  get('stop-download').handlers.click();await flush();
  assert.ok(requests.some(request=>request.path==='/api/data-suite/verify-stop'));
  snapshot={...snapshot,controls_available:false};timer();await flush();
  assert.equal(get('start-download').disabled,true,'Read-only monitor cannot start work');
  assert.equal(get('process-saved-data').disabled,true);
  console.log('Data suite UI tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
