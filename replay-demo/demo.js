"use strict";

// Static files only. The full application and its API are not loaded here.
const app = {replay:null,frameIndex:0,playing:false,speed:1,animationId:null,lastTick:0,
  chartCache:new Map(),orderState:new Map(),orderStateFrame:-1,frontendTimings:null};
const $ = id => document.getElementById(id);
const price = value => value==null?"—":Number(value).toFixed(6);
const recordings = new Map(), loadedFiles = new Map();
let requestRevision=0;
const shapeName = value => ({spot:"Spot",curve:"Curve",bid_ask:"Bid-Ask"}[value]||value);
const money = value => `${Number(value).toLocaleString(undefined,{minimumFractionDigits:4,maximumFractionDigits:6})} USDC`;
const units = (value,unit) => `${value} ${unit}`;

function pair(target,name,value){
  const key=document.createElement('dt'),text=document.createElement('dd');key.textContent=name;text.textContent=String(value);target.append(key,text);
}
function showConfiguration(record){
  const config=record.configuration,target=$('configuration');target.replaceChildren();
  for(const [name,value] of [['Shape',shapeName(config.shape)],['Fee / flip spread',`${config.fee_percent}%`],
    ['Minimum price',units(config.minimum_price,'USDC')],['Maximum price',units(config.maximum_price,'USDC')],
    ['MON amount',units(config.mon_amount,'MON')],['USDC amount',units(config.usdc_amount,'USDC')],
    ['From block',record.interval.from_block],['To block',record.interval.to_block]])pair(target,name,value);
  $('configuration-json').textContent=JSON.stringify(config,null,2);
}
function syncPlayButton(){
  $('play-button').setAttribute('aria-label',app.playing?'Pause replay':'Play replay');
  $('play-button').setAttribute('aria-pressed',String(app.playing));
}
// These wrappers add accessible state to the copied local playback controls.
const originalStopPlayback=stopPlayback;
stopPlayback=function(){originalStopPlayback();syncPlayButton();};
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
  $('recorded-results').hidden=true;$('empty-state').hidden=false;
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
  const revision=++requestRevision,id=$('recording-select').value,record=recordings.get(id);
  clearReplay();$('load-error').hidden=true;$('load-replay').disabled=true;$('load-status').textContent='Loading recorded replay…';
  try{
    if(!record)throw new Error('Select a valid recorded configuration.');
    const run=await readRecording(record);if(revision!==requestRevision)return;
    app.replay={frames:run.compact_timeline,results:run.results,run_id:run.run_id,timeline_sha256:run.timeline_sha256};
    app.frameIndex=0;app.orderStateFrame=-1;app.orderState.clear();
    $('run-title').textContent=`${shapeName(run.normalized_configuration.shape)} · MON / USDC`;
    $('engine-version').textContent=run.engine_version;showResults(run);
    $('empty-state').hidden=true;$('recorded-results').hidden=false;
    $('scrubber').max=String(app.replay.frames.length-1);$('total-frames').textContent=app.replay.frames.length.toLocaleString();
    renderFrame(0);$('load-status').textContent=`Loaded ${run.run_id} · ${record.frames} verified frames`;
  }catch(error){if(revision===requestRevision){clearReplay();$('load-error').textContent=error.message;$('load-error').hidden=false;$('load-status').textContent='Replay could not be loaded.';}}
  finally{if(revision===requestRevision)$('load-replay').disabled=false;}
}
function selectionChanged(){
  requestRevision++;clearReplay();$('load-error').hidden=true;
  const record=recordings.get($('recording-select').value);
  $('load-replay').disabled=!record;
  if(record){showConfiguration(record);$('load-status').textContent='Recorded configuration selected. Select Load Replay.';}
}
const welcomeKey='kurulab.replay-demo.welcome.v1';
function dismissWelcome(){
  $('welcome-modal').close();try{localStorage.setItem(welcomeKey,'seen');}catch{}
  $('recording-select').focus();
}
$('explore-demo').addEventListener('click',dismissWelcome);
$('welcome-modal').addEventListener('cancel',event=>{event.preventDefault();dismissWelcome();});
$('welcome-modal').addEventListener('keydown',event=>{
  if(event.key!=='Tab')return;
  const first=$('explore-demo'),last=$('welcome-github');
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
});
$('about-demo').addEventListener('click',()=>$('welcome-modal').showModal());
try{if(localStorage.getItem(welcomeKey)!=='seen')$('welcome-modal').showModal();}catch{$('welcome-modal').showModal();}
$('load-replay').addEventListener('click',loadReplay);$('recording-select').addEventListener('change',selectionChanged);
$('play-button').addEventListener('click',()=>{togglePlayback();syncPlayButton();});
$('scrubber').addEventListener('input',event=>{stopPlayback();renderFrame(Number(event.target.value));});
document.querySelectorAll('.speed-button').forEach(button=>button.addEventListener('click',()=>{
  app.speed=Number(button.dataset.speed);
  document.querySelectorAll('.speed-button').forEach(item=>{const selected=item===button;item.classList.toggle('active',selected);item.setAttribute('aria-pressed',String(selected));});
}));
let resizeFrame=0;window.addEventListener('resize',()=>{if(!resizeFrame)resizeFrame=requestAnimationFrame(()=>{resizeFrame=0;if(app.replay)renderFrame(app.frameIndex);});});
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopPlayback();});
async function init(){
  try{
    const response=await fetch('data/catalog.json');if(!response.ok)throw new Error(`Recording catalog unavailable (HTTP ${response.status}).`);
    const catalog=await response.json();if(catalog.schema_version!==1||!catalog.recordings?.length)throw new Error('No verified recordings are available.');
    const select=$('recording-select');select.replaceChildren();
    for(const record of catalog.recordings){recordings.set(record.run_id,record);const option=document.createElement('option');option.value=record.run_id;option.textContent=record.label;select.append(option);}
    select.value=catalog.default_run_id;select.disabled=false;selectionChanged();
  }catch(error){$('load-error').textContent=error.message;$('load-error').hidden=false;$('load-status').textContent='Demo data could not be loaded.';}
}
init();
