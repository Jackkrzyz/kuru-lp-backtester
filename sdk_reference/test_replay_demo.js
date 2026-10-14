// Exercise the shipped static controller and copied renderer with real exports.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const directory=path.resolve(__dirname,'../replay-demo');
class Element{
  constructor(){this.children=[];this.handlers={};this.attributes={};this.hidden=false;this.value='';this.dataset={};this.style={};this.classList={toggle(){}};this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}get textContent(){return this._text+this.children.map(item=>item.textContent).join('');}
  append(...items){this.children.push(...items);}replaceChildren(){this._text='';this.children=[];}
  addEventListener(name,handler){this.handlers[name]=handler;}setAttribute(name,value){this.attributes[name]=value;}
  focus(){this.focused=true;}showModal(){this.open=true;}close(){this.open=false;}
  getBoundingClientRect(){return {width:800,height:240};}
  getContext(){return this.context ||= {calls:[],setTransform(){},clearRect(){},beginPath(){},moveTo(x,y){this.calls.push(['move',x,y]);},lineTo(x,y){this.calls.push(['line',x,y]);},stroke(){},fillText(){},drawImage(){},arc(){throw new Error('Price-chart fill dots must not return');},fill(){}};}
}
const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
const speeds=[1,10,50,100].map(speed=>{const button=new Element();button.dataset.speed=String(speed);return button;});
const requests=[],saved=new Map(),animations=new Map();let animationId=0,failure=null;
const sandbox={window:{devicePixelRatio:1,addEventListener(){}},document:{hidden:false,getElementById:get,createElement:()=>new Element(),
  querySelectorAll:()=>speeds,addEventListener(){}},localStorage:{getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)},
  crypto:webcrypto,TextDecoder,performance:{now:()=>1},requestAnimationFrame:fn=>{animations.set(++animationId,fn);return animationId;},cancelAnimationFrame:id=>animations.delete(id),
  fetch:async url=>{requests.push(url);assert.ok(/^data\/(catalog|lp-[0-9a-f]{20})\.json$/.test(url),'No backend or external requests');
    if(url===failure)return {ok:false,status:404};const bytes=fs.readFileSync(path.join(directory,url));
    return {ok:true,json:async()=>JSON.parse(bytes),arrayBuffer:async()=>Uint8Array.from(bytes).buffer};},console};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(directory,'assets/replay-renderer.js'),'utf8'),sandbox);
vm.runInContext(fs.readFileSync(path.join(directory,'demo.js'),'utf8'),sandbox);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const evaluate=code=>vm.runInContext(code,sandbox);
async function settle(){for(let i=0;i<12;i++)await flush();}
(async()=>{
  await settle();assert.equal(get('welcome-modal').open,true);
  get('explore-demo').handlers.click();assert.equal(get('welcome-modal').open,false);
  assert.equal(saved.get('kurulab.replay-demo.welcome.v2'),'seen');
  const catalog=JSON.parse(fs.readFileSync(path.join(directory,'data/catalog.json')));
  for(const record of catalog.recordings){
    get('recording-select').value=record.run_id;get('recording-select').handlers.change();
    assert.equal(evaluate('app.replay'),null,'Selecting a different recording clears the previous result');
    assert.equal(get('configuration-json').textContent,JSON.stringify(record.configuration,null,2));
    await get('load-replay').handlers.click();
    assert.equal(get('load-error').hidden,true,get('load-error').textContent);
    assert.equal(evaluate('app.replay.run_id'),record.run_id);
    assert.equal(JSON.stringify(evaluate('app.replay.results')),JSON.stringify(record.results));
    assert.equal(get('recorded-results').hidden,false);
    get('play-button').handlers.click();assert.equal(evaluate('app.playing'),true);
    assert.equal(get('play-button').attributes['aria-label'],'Pause replay');
    const tick=animations.get(evaluate('app.animationId'));tick(1000);
    assert.equal(evaluate('app.frameIndex'),1,'Play advances the recorded timeline');
    get('play-button').handlers.click();assert.equal(evaluate('app.playing'),false);
    assert.equal(get('play-button').attributes['aria-label'],'Play replay');
    speeds[3].handlers.click();assert.equal(evaluate('app.speed'),100);assert.equal(evaluate('playbackRate()'),100);
    get('scrubber').value=String(record.frames-1);get('scrubber').handlers.input({target:get('scrubber')});
    assert.equal(evaluate('app.frameIndex'),record.frames-1);
    const original=JSON.parse(fs.readFileSync(path.join(directory,record.file)));
    const fills=original.compact_timeline.flatMap(frame=>frame.fills);
    assert.equal(evaluate('replayFillsThrough(app.frameIndex).length'),fills.length);
    for(const fill of fills.slice(-12)) {
      assert.ok(get('replay-fills').textContent.includes(`${fill.size} MON @ ${fill.price} USDC`));
      assert.ok(get('replay-fills').textContent.includes(fill.timestamp));
    }
    const context=get('price-chart').getContext();context.calls=[];evaluate('drawPriceChart()');
    const ticks=context.calls.filter(call=>call[0]==='move'&&(call[2]===196||call[2]===211));
    assert.equal(ticks.length,fills.length,'One execution tick per recorded fill');
    const frames=original.compact_timeline,start=Date.parse(frames[0].timestamp),end=Date.parse(frames.at(-1).timestamp);
    fills.forEach((fill,index)=>{
      const expected=58+(Date.parse(fill.timestamp)-start)/Math.max(1,end-start)*(800-58-14);
      assert.ok(Math.abs(ticks[index][1]-expected)<1e-8,'Execution uses its own timestamp, not frame spacing');
      assert.equal(ticks[index][2],['buy','bid'].includes(fill.side)?196:211,'Correct side lane');
    });
    assert.equal(get('current-frame').textContent,record.frames.toLocaleString());
    get('scrubber').value='0';get('scrubber').handlers.input({target:get('scrubber')});
    assert.equal(evaluate('app.frameIndex'),0,'Backward scrubbing is supported');
    assert.equal(evaluate('replayFillsThrough(0).length'),original.compact_timeline[0].fills.length);
  }
  const missing=catalog.recordings[0];failure=missing.file;evaluate('loadedFiles.clear()');
  get('recording-select').value=missing.run_id;get('recording-select').handlers.change();await get('load-replay').handlers.click();
  assert.equal(get('load-error').hidden,false);assert.ok(get('load-error').textContent.includes('HTTP 404'));
  assert.equal(evaluate('app.replay'),null);assert.equal(get('recorded-results').hidden,true,'A missing file cannot show an old or placeholder result');
  assert.equal(requests.filter(url=>url==='data/catalog.json').length,1);
  get('about-demo').handlers.click();get('welcome-modal').handlers.cancel({preventDefault(){}});
  assert.equal(get('welcome-modal').open,false,'Escape dismissal is handled');
  console.log(`Static demo integration passed: ${catalog.recordings.length} real recordings, playback, pause, speed, scrub, visible 404, static requests only`);
})().catch(error=>{console.error(error);process.exitCode=1;});
