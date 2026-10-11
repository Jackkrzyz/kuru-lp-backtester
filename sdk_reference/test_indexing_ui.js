/* Test real-state rendering and bounded animation without a browser or server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(){this.textContent='';this.children=[];this.style={};this.clientWidth=900;this.clientHeight=330;}
  animate(keyframes,options){const animation={element:this,keyframes,options,cancelled:false,cancel(){this.cancelled=true;}};animations.push(animation);return animation;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];this.textContent='';this.replacements=(this.replacements||0)+1;}
  setAttribute(key,value){this[key]=value;}
}
const elements = new Map();
const animations=[];
const get = id => {if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
let frames=0, timer, response, requests=0, now=Date.now();
const media = {matches:false};
const context={document:{getElementById:get,createElement:()=>new Element(),createElementNS:()=>new Element()},
  location:{pathname:'/',hostname:'127.0.0.1'},matchMedia:()=>media,devicePixelRatio:1,
  performance:{now:()=>1000},Date:class extends Date {static now(){return now;}},Number,JSON,Math,AbortSignal,
  requestAnimationFrame:()=>++frames,cancelAnimationFrame(){},
  ResizeObserver:class {observe(){}},
  setTimeout:callback=>{timer=callback;},
  fetch:async url=>{assert.equal(url,'/api/indexing-progress');requests++;return {ok:true,json:async()=>response};}};
const config={from_block:10,to_block:20,market:'0xabc',download_only:true};
response={phase:'downloading',configuration:config,updated_at:Date.now()/1000,downloaded_events:10,reconstructed_events:0,validated_events:0,downloaded_block:11,event_counts:{Trade:2}};
vm.runInNewContext(fs.readFileSync(require.resolve('../frontend/indexing.js'),'utf8'),context);
async function flush(){await new Promise(resolve=>setImmediate(resolve));}
(async()=>{
  await flush();
  assert.equal(get('downloaded').textContent,'10');
  assert.equal(get('phase').textContent,'Recording market logs with Envio');
  assert.equal(frames,0,'Loading a saved state must not invent movement');
  assert.equal(animations.length,0);
  assert.equal(get('flow-range').textContent,'Blocks 10–20');
  assert.equal(get('flow-l3').textContent,'Not run · download only');
  assert.equal(get('flow-verify').textContent,'Not run · download only');
  response={...response,downloaded_events:40,downloaded_block:14,updated_at:response.updated_at+.1};timer();await flush();
  assert.equal(get('downloaded').textContent,'40');assert.equal(frames,0,'Do not invent packet animation from aggregate counts');
  assert.equal(animations.length,1);
  assert.equal(animations[0].element,get('flow-archive-box'),'A saved-count change highlights storage only');
  assert.equal(get('flow-activity').textContent,'+30 records saved since the last update');
  response={...response,actions:[{kind:'request',sequence:1,timestamp:1,from_block:10,to_block_exclusive:21},
    {kind:'page_committed',sequence:2,timestamp:2,page_events:31,new_events:30,next_block:15,committed_bytes:1000,event_counts:{Trade:3,OrderCreated:27}}],action_sequence:2};
  timer();await flush();
  assert.equal(get('action-trace').children.length,2);
  assert.equal(get('action-trace').children[0].children[1].children[0].textContent,'Commit the page and cursor');
  assert.ok(get('action-trace').children[0].children[1].children[1].textContent.includes('1 duplicates skipped'));
  assert.ok(animations.some(a=>a.element===get('flow-request-packet')));
  const unchanged=animations.length, rebuilds=get('action-trace').replacements;timer();await flush();
  assert.equal(animations.length,unchanged,'An unchanged snapshot must not replay actions');
  assert.equal(get('action-trace').replacements,rebuilds,'An unchanged snapshot must not rebuild the trace');
  response={...response,last_response:{log_count:12,block_count:3,next_block:16},last_committed_page:{next_block:15},
    actions:[...response.actions,{kind:'response',sequence:3,timestamp:3,log_count:12,block_count:3,next_block:16,decoded_response_bytes:2000}],action_sequence:3};
  timer();await flush();
  assert.equal(get('flow-page').textContent,'12 logs · 3 blocks');
  assert.equal(get('flow-cursor').textContent,'15','The durable cursor must not use the response cursor');
  assert.equal(animations.at(-1).element,get('flow-response-packet'));
  const beforeAnimations=animations.length;
  const before=frames;
  response={...response,phase:'rate_limit_wait',downloaded_events:80,retry_seconds:10,updated_at:now/1000-2};timer();await flush();
  assert.equal(get('phase').textContent,'Waiting for the request allowance');assert.equal(frames,before,'No moving data during a rate-limit wait');
  assert.equal(animations.length,beforeAnimations);assert.ok(animations.every(a=>a.cancelled));
  assert.ok(get('flow-activity').textContent.includes('retry in 8 s'));
  const waitingRebuilds=get('action-trace').replacements;
  now+=3000;timer();await flush();
  assert.ok(get('flow-activity').textContent.includes('retry in 5 s'));
  assert.equal(get('action-trace').replacements,waitingRebuilds,'Countdown changes without rebuilding the trace');
  response={...response,phase:'failed',error:'Contract not supported',updated_at:response.updated_at+.1};timer();await flush();
  assert.equal(get('error').textContent,'Contract not supported');assert.equal(get('error').hidden,false);
  media.matches=true;
  response={...response,phase:'downloading',error:null,downloaded_events:100,updated_at:response.updated_at+.1};timer();await flush();
  assert.equal(frames,before,'Reduced-motion mode must not schedule animated particles');
  assert.equal(animations.length,beforeAnimations);
  media.matches=false;
  response={...response,updated_at:Date.now()/1000-30,downloaded_events:140};timer();await flush();
  assert.equal(get('phase').textContent,'No recent update');
  assert.equal(animations.length,beforeAnimations,'Stale counters do not trigger motion');
  response={...response,configuration:{...config,from_block:12},updated_at:Date.now()/1000};timer();await flush();
  assert.equal(animations.length,beforeAnimations,'A different job starts without replaying saved actions');
  response={phase:'idle'};timer();await flush();
  assert.equal(get('downloaded').textContent,'0');assert.equal(get('scope').textContent,'—');
  assert.equal(get('coverage').children.length,0);
  assert.ok(requests>=5);
  console.log('Indexing UI tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
