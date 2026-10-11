/* Test real-state rendering and bounded animation without a browser or server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(){this.textContent='';this.children=[];this.style={};this.clientWidth=900;this.clientHeight=330;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(){this.children=[];this.textContent='';}
  setAttribute(key,value){this[key]=value;}
}
const elements = new Map();
const canvasContext = new Proxy({createRadialGradient:()=>({addColorStop(){}})}, {get:(target,key)=>target[key]||(()=>{})});
const get = id => {if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
get('flow').getContext=()=>canvasContext;
let frames=0, timer, response, requests=0;
const media = {matches:false};
const context={document:{getElementById:get,createElement:()=>new Element(),createElementNS:()=>new Element()},
  location:{pathname:'/',hostname:'127.0.0.1'},matchMedia:()=>media,devicePixelRatio:1,
  performance:{now:()=>1000},Date,Number,JSON,Math,AbortSignal,
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
  assert.equal(get('phase').textContent,'Recording');
  assert.equal(frames,0,'Loading a saved state must not invent movement');
  response={...response,downloaded_events:40,downloaded_block:14,updated_at:response.updated_at+.1};timer();await flush();
  assert.equal(get('downloaded').textContent,'40');assert.ok(frames>0,'New real events should cause movement');
  const before=frames;
  response={...response,phase:'rate_limit_wait',downloaded_events:80,updated_at:response.updated_at+.1};timer();await flush();
  assert.equal(get('phase').textContent,'Rate limited');assert.equal(frames,before,'No moving data during a rate-limit wait');
  response={...response,phase:'failed',error:'Contract not supported',updated_at:response.updated_at+.1};timer();await flush();
  assert.equal(get('error').textContent,'Contract not supported');assert.equal(get('error').hidden,false);
  media.matches=true;
  response={...response,phase:'downloading',error:null,downloaded_events:100,updated_at:response.updated_at+.1};timer();await flush();
  assert.equal(frames,before,'Reduced-motion mode must not schedule animated particles');
  response={...response,updated_at:Date.now()/1000-30,downloaded_events:140};timer();await flush();
  assert.equal(get('phase').textContent,'No recent update');
  response={phase:'idle'};timer();await flush();
  assert.equal(get('downloaded').textContent,'0');assert.equal(get('scope').textContent,'—');
  assert.equal(get('coverage').children.length,0);
  assert.ok(requests>=5);
  console.log('Indexing UI tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
