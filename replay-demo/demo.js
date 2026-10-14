"use strict";

// Static files only. The full application and its API are not loaded here.
const app = {replay:null,frameIndex:0,playing:false,speed:100,animationId:null,lastTick:0,
  chartCache:new Map(),orderState:new Map(),orderStateFrame:-1,frontendTimings:null};
const $ = id => document.getElementById(id);
const price = value => value==null?"—":Number(value).toFixed(6);
const recordings = new Map(), loadedFiles = new Map();
let requestRevision=0;
let launchTimer=null;
let catalogReady=false;
let selectionChosen=false,sceneTimer=null,sceneRevision=0;
const shapeDescription = value => ({spot:'Even liquidity across the range.',curve:'More liquidity near the market price.',bid_ask:'More liquidity at the range edges.'}[value]);
const shapeName = value => ({spot:"Spot",curve:"Curve",bid_ask:"Bid-Ask"}[value]||value);
const money = value => `${Number(value).toLocaleString(undefined,{minimumFractionDigits:4,maximumFractionDigits:6})} USDC`;

function pair(target,name,value){
  const key=document.createElement('dt'),text=document.createElement('dd');key.textContent=name;text.textContent=String(value);target.append(key,text);
}
// Use saved SDK orders. Bar heights are quote notional on both sides.
function orderGrid(record){
  const grid=record.initial_order_grid;
  const orders=[...grid.orders].sort((a,b)=>Number(a.price_raw)-Number(b.price_raw));
  const minimum=Number(grid.minimum_price_raw),span=Math.max(1,Number(grid.maximum_price_raw)-minimum);
  const peak=Math.max(1,...orders.map(order=>Number(order.quote_notional_raw)));
  const width=Math.min(18,280/Math.max(1,orders.length));
  const bars=orders.map(order=>{
    const x=16+(Number(order.price_raw)-minimum)/span*328;
    const height=Number(order.quote_notional_raw)/peak*90;
    return `<rect x="${x-width/2}" y="${112-height}" width="${width}" height="${height}" rx="2" fill="${order.side==='buy'?'#8894ff':'#f2aa79'}"/>`;
  }).join('');
  const center=16+(Number(grid.current_market_price_raw)-minimum)/span*328;
  return `<svg viewBox="0 0 360 138" role="img" aria-label="${shapeName(record.configuration.shape)} saved order grid; blue bids and orange asks"><path d="M16 112H344" stroke="#ffffff25"/><path d="M${center} 14V116" stroke="#ffffff55" stroke-dasharray="3 4"/>${bars}<text x="16" y="133" fill="#aab0b6" font-size="10">${record.configuration.minimum_price}</text><text x="344" y="133" text-anchor="end" fill="#aab0b6" font-size="10">${record.configuration.maximum_price}</text></svg>`;
}
function showShapeOptions(){
  const target=$('shape-options');target.replaceChildren();
  for(const record of recordings.values()){
    const button=document.createElement('button');button.type='button';button.className='shape-window';
    button.dataset.runId=record.run_id;button.setAttribute('aria-pressed','false');
    const heading=document.createElement('strong');heading.textContent=shapeName(record.configuration.shape);
    const description=document.createElement('span');description.textContent=shapeDescription(record.configuration.shape);
    const grid=document.createElement('div');grid.className='shape-grid';grid.innerHTML=orderGrid(record);
    const config=document.createElement('small');config.textContent=`±${record.configuration.lower_range_percent}% range · ${record.configuration.fee_percent}% spread`;
    const funding=document.createElement('small');funding.className='shape-funding';
    funding.textContent=`${Number(record.configuration.mon_amount).toLocaleString()} MON + ${Number(record.configuration.usdc_amount).toLocaleString()} USDC`;
    button.append(heading,description,grid,config,funding);
    button.addEventListener('click',()=>{$('recording-select').value=record.run_id;selectionChanged();});target.append(button);
  }
}
function setSpeed(speed){
  app.speed=speed;app.lastTick=0;$('replay-speed').textContent=`${speed}×`;
  document.querySelectorAll('.speed-button').forEach(button=>{
    const selected=Number(button.dataset.speed)===speed;button.classList.toggle('active',selected);button.setAttribute('aria-pressed',String(selected));
  });
}
function cancelLaunch(){clearTimeout(launchTimer);launchTimer=null;}
function showScene(replay){
  clearTimeout(sceneTimer);sceneTimer=null;
  const revision=++sceneRevision;
  const incoming=$(replay?'replay-scene':'selection-scene');
  const outgoing=$(replay?'selection-scene':'replay-scene');
  const wasVisible=!outgoing.hidden;
  incoming.classList.remove('scene-exit','scene-enter');
  outgoing.classList.remove('scene-exit','scene-enter');
  incoming.hidden=false;incoming.inert=false;incoming.setAttribute('aria-hidden','false');
  outgoing.inert=true;outgoing.setAttribute('aria-hidden','true');
  incoming.classList.add('scene-enter');
  if(wasVisible&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches){
    outgoing.classList.add('scene-exit');
    sceneTimer=setTimeout(()=>{if(revision===sceneRevision){outgoing.hidden=true;outgoing.classList.remove('scene-exit');sceneTimer=null;}},400);
  }else outgoing.hidden=true;
  incoming.scrollIntoView({behavior:'instant',block:'start'});
  if(replay)$('run-title').focus();
  else (document.querySelector('.shape-window.selected')||document.querySelector('.shape-window'))?.focus();
}
function scheduleReplay(){
  cancelLaunch();
  if(!catalogReady||!selectionChosen||$('welcome-modal').open||document.hidden)return;
  const revision=requestRevision;
  launchTimer=setTimeout(()=>{launchTimer=null;if(revision===requestRevision&&!document.hidden&&!$('welcome-modal').open)loadReplay();},1100);
}
function showConfiguration(record){
  $('configuration-json').textContent=JSON.stringify(record.configuration,null,2);
  document.querySelectorAll('.shape-window').forEach(button=>{
    const selected=button.dataset.runId===record.run_id;button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));
  });
}
function syncPlayButton(){
  $('play-button').setAttribute('aria-label',app.playing?'Pause replay':'Play replay');
  $('play-button').setAttribute('aria-pressed',String(app.playing));
}
// These wrappers add accessible state to the copied local playback controls.
const originalStopPlayback=stopPlayback;
stopPlayback=function(){originalStopPlayback();syncPlayButton();};
// Accumulate elapsed time so 100 frames per second also works on a 60 Hz screen.
playbackLoop=function(timestamp){
  if(!app.playing||!app.replay)return;
  if(!app.lastTick)app.lastTick=timestamp;
  const steps=Math.floor((timestamp-app.lastTick)*playbackRate()/1000);
  if(steps>0){
    app.lastTick+=steps*1000/playbackRate();
    renderFrame(Math.min(app.frameIndex+steps,app.replay.frames.length-1));
    if(app.frameIndex===app.replay.frames.length-1){stopPlayback();return;}
  }
  app.animationId=requestAnimationFrame(playbackLoop);
};
const originalRenderFrame=renderFrame;
renderFrame=function(index){
  originalRenderFrame(index);
  if(!app.replay)return;
  const frame=app.replay.frames[app.frameIndex];
  $('scrubber').setAttribute('aria-valuetext',`${frame.timestamp}, block ${frame.block}, frame ${app.frameIndex+1} of ${app.replay.frames.length}`);
  $('frame-balances').textContent=`Cash ${money(frame.cash)} · MON ${frame.inventory} · Portfolio ${money(frame.marked_value)} · P&L ${money(frame.cumulative_pnl)}`;
};
function clearReplay(){
  stopPlayback();app.replay=null;app.chartCache.clear();app.orderState.clear();app.orderStateFrame=-1;
  $('recorded-results').hidden=true;
}
function showResults(run){
  const r=run.results, metrics=$('metrics');metrics.replaceChildren();
  for(const [name,value] of [['Position P&L',money(r.strategy_pnl)],['Hold P&L',money(r.hold_pnl)],
    ['Excess P&L',money(r.excess_pnl)],['Maximum drawdown',money(r.maximum_drawdown)],
    ['Turnover',money(r.turnover)],['Buy / sell fills',`${r.buy_fill_count} / ${r.sell_fill_count}`],
    ['Ending MON',`${Number(run.final_mon).toLocaleString(undefined,{maximumFractionDigits:6})} MON`],['Ending USDC',money(run.final_usdc)],
    ['Earned flip spread',money(run.earned_flip_spread)],['Time in range',`${r.time_in_range_percent.toFixed(2)}%`],
    ['Capital utilization',`${r.capital_utilization_percent.toFixed(2)}%`],
    ['Initial / flip orders',`${run.generated_initial_bid_count+run.generated_initial_ask_count} / ${run.flip_orders_created}`],
    ['Maker / action fees',`${money(run.maker_fees)} / ${money(run.action_fees)}`],
    ['Gross P&L',money(run.gross_pnl)],['Required MON / USDC ratio',Number(r.required_mon_usdc_ratio).toFixed(6)]]){
    const box=document.createElement('article'),label=document.createElement('span'),valueNode=document.createElement('strong');
    box.className='metric';label.textContent=name;valueNode.textContent=value;box.append(label,valueNode);metrics.append(box);
  }
  const warning=$('impact-warning');warning.hidden=!r.transaction_impact_warning;
  warning.textContent=r.transaction_impact_warning?`Material counterfactual impact: simulated fills reached ${r.maximum_transaction_impact_percent.toFixed(2)}% of one historical transaction. This recording may materially change the real execution path. Recorded P&L is not a reliable estimate of live profit for this position.`:'';
  const details=$('recording-details');details.replaceChildren();
  for(const [name,value] of [['Run ID',run.run_id],['Engine',run.engine_version],['Summary SHA256',run.summary_sha256],
    ['Replay SHA256',run.timeline_sha256],['Market',run.normalized_configuration.market],
    ['Assumption',run.counterfactual.assumption],['Maximum transaction impact',`${r.maximum_transaction_impact_percent}%`]])pair(details,name,value);
}
function stable(value){return JSON.stringify(value&&typeof value==='object'?Array.isArray(value)?value.map(item=>JSON.parse(stable(item))):Object.fromEntries(Object.keys(value).sort().map(key=>[key,JSON.parse(stable(value[key]))])):value);}
async function readRecording(record){
  if(loadedFiles.has(record.run_id))return loadedFiles.get(record.run_id);
  if(!/^data\/lp-[0-9a-f]{20}\.json$/.test(record.file))throw new Error('Invalid recording path.');
  const response=await fetch(record.file);
  if(!response.ok)throw new Error(`Recording unavailable (HTTP ${response.status}). No replay was loaded.`);
  const buffer=await response.arrayBuffer();
  const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',buffer)),byte=>byte.toString(16).padStart(2,'0')).join('');
  if(hash!==record.sha256)throw new Error('Recording integrity check failed. No replay was loaded.');
  const run=JSON.parse(new TextDecoder().decode(buffer));
  if(run.run_id!==record.run_id||stable(run.normalized_configuration)!==stable(record.configuration)||stable(run.results)!==stable(record.results)||
    run.summary_sha256!==record.summary_sha256||run.timeline_sha256!==record.timeline_sha256||run.compact_timeline.length!==record.frames)
    throw new Error('Recording does not match its saved configuration and results.');
  loadedFiles.set(record.run_id,run);return run;
}
async function loadReplay(){
  cancelLaunch();
  const revision=++requestRevision,id=$('recording-select').value,record=recordings.get(id);
  clearReplay();$('load-error').hidden=true;$('load-status').textContent='Loading recorded replay…';
  try{
    if(!record)throw new Error('Select a valid recorded configuration.');
    const run=await readRecording(record);if(revision!==requestRevision)return;
    app.replay={frames:run.compact_timeline,results:run.results,run_id:run.run_id,timeline_sha256:run.timeline_sha256};
    app.frameIndex=0;app.orderStateFrame=-1;app.orderState.clear();
    $('run-title').textContent=`${shapeName(run.normalized_configuration.shape)} · MON / USDC`;
    $('engine-version').textContent=run.engine_version;showResults(run);
    $('recorded-results').hidden=false;
    $('scrubber').max=String(app.replay.frames.length-1);$('total-frames').textContent=app.replay.frames.length.toLocaleString();
    showScene(true);renderFrame(0);setSpeed(100);
    if(!document.hidden&&!$('welcome-modal').open){
      togglePlayback();syncPlayButton();
    }
    $('load-status').textContent=`${shapeName(record.configuration.shape)} · ${record.frames} verified frames · Replay at 100×`;
  }catch(error){if(revision===requestRevision){clearReplay();showScene(false);$('load-error').textContent=error.message;$('load-error').hidden=false;$('load-status').textContent='Replay could not be loaded.';}}
}
function selectionChanged(){
  cancelLaunch();
  requestRevision++;clearReplay();$('load-error').hidden=true;
  const record=recordings.get($('recording-select').value);
  selectionChosen=Boolean(record);
  if(record){showConfiguration(record);$('load-status').textContent='Grid ready. Replay starts at 100×.';scheduleReplay();}
}
const welcomeKey='kurulab.replay-demo.welcome.v2';
function dismissWelcome(){
  $('welcome-modal').close();try{localStorage.setItem(welcomeKey,'seen');}catch{}
  (document.querySelector('.shape-window.selected')||document.querySelector('.shape-window'))?.focus();
  if(selectionChosen&&!app.replay)scheduleReplay();
}
$('explore-demo').addEventListener('click',dismissWelcome);
$('welcome-modal').addEventListener('cancel',event=>{event.preventDefault();dismissWelcome();});
$('welcome-modal').addEventListener('keydown',event=>{
  if(event.key!=='Tab')return;
  const first=$('explore-demo'),last=$('welcome-github');
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
});
$('about-demo').addEventListener('click',()=>{cancelLaunch();stopPlayback();$('welcome-modal').showModal();});
try{if(localStorage.getItem(welcomeKey)!=='seen')$('welcome-modal').showModal();}catch{$('welcome-modal').showModal();}
$('recording-select').addEventListener('change',selectionChanged);
$('choose-shape').addEventListener('click',()=>{
  cancelLaunch();requestRevision++;stopPlayback();selectionChosen=false;
  $('load-error').hidden=true;
  $('load-status').textContent='Select a shape to start its replay at 100×.';
  showScene(false);
});
$('play-button').addEventListener('click',()=>{togglePlayback();syncPlayButton();});
$('scrubber').addEventListener('input',event=>{stopPlayback();renderFrame(Number(event.target.value));});
document.querySelectorAll('.speed-button').forEach(button=>button.addEventListener('click',()=>setSpeed(Number(button.dataset.speed))));
let resizeFrame=0;window.addEventListener('resize',()=>{if(!resizeFrame)resizeFrame=requestAnimationFrame(()=>{resizeFrame=0;if(app.replay)renderFrame(app.frameIndex);});});
document.addEventListener('visibilitychange',()=>{if(document.hidden){cancelLaunch();stopPlayback();}});
async function init(){
  try{
    const response=await fetch('data/catalog.json');if(!response.ok)throw new Error(`Recording catalog unavailable (HTTP ${response.status}).`);
    const catalog=await response.json();if(catalog.schema_version!==1||!catalog.recordings?.length)throw new Error('No verified recordings are available.');
    const select=$('recording-select');select.replaceChildren();
    for(const record of catalog.recordings){recordings.set(record.run_id,record);const option=document.createElement('option');option.value=record.run_id;option.textContent=record.label;select.append(option);}
    await Promise.all(catalog.recordings.map(async record=>{
      const run=await readRecording(record);record.initial_order_grid=run.initial_order_grid;
    }));
    showShapeOptions();catalogReady=true;select.value='';select.disabled=false;
    $('load-status').textContent='Select a shape to start its replay at 100×.';
  }catch(error){$('load-error').textContent=error.message;$('load-error').hidden=false;$('load-status').textContent='Demo data could not be loaded.';}
}
init();
