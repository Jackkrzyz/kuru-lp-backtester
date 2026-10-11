/* A schematic of the real data path. Pulses represent observed batches, not individual logs. */
(() => {
  const $=id=>document.getElementById(id), number=value=>Number(value||0).toLocaleString();
  let seen=null, rendered=null;
  const pulses=new Map();
  function stopPulses() {for(const animation of pulses.values())animation.cancel();pulses.clear();}
  function pulse(id,distance) {
    if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
    pulses.get(id)?.cancel();
    const frames=distance===undefined?[{stroke:'#ff874a',fill:'#523626'},{stroke:'#45413e',fill:'#242322'}]:
      [{opacity:0,transform:'translateX(0px)'},{opacity:1,offset:.15},{opacity:1,offset:.8},{opacity:0,transform:`translateX(${distance}px)`}];
    pulses.set(id,$(id).animate(frames,{duration:850,easing:'ease-out'}));
  }
  function renderFlow(state,stale,job) {
    const config=state.configuration,phase=state.phase||'idle',sameJob=seen?.job===job;
    const running=!stale&&['downloading','reconstructing','validating','rpc_parity'].includes(phase);
    if(!running||!sameJob||matchMedia('(prefers-reduced-motion: reduce)').matches)stopPulses();
    $('flow-mode').textContent=config?(config.download_only?'DOWNLOAD ONLY':'DOWNLOAD + VERIFY'):'No job selected';
    const market=config?.market;
    $('flow-market').textContent=market?market.slice(0,10)+'…'+market.slice(-6):'No market selected';
    $('flow-range').textContent=config?`Blocks ${config.from_block}–${config.to_block}`:'Block range —';
    $('flow-page').textContent=state.last_response?`${number(state.last_response.log_count)} logs · ${number(state.last_response.block_count)} blocks`:'Response page: not reported';
    $('flow-requests').textContent=`${number(state.requests)} requests · ${number(state.pages)} saved pages`;
    $('flow-cursor').textContent=state.last_committed_page?number(state.last_committed_page.next_block):state.pages>0?number(Number(state.downloaded_block)+1):'—';
    $('flow-l3').textContent=config?.download_only?'Not run · download only':`${number(state.reconstructed_events)} events applied`;
    $('flow-verify').textContent=config?.download_only?'Not run · download only':state.event_parity_status?`Event parity: ${state.event_parity_status}`:'Not checked';
    const retry=Math.max(0,Math.ceil(Number(state.retry_seconds||0)-Math.max(0,Date.now()/1000-Number(state.updated_at||0))));
    let activity=stale?'No recent update. Motion is paused.':phase==='rate_limit_wait'?`HTTP 429 · ${retry>0?'retry in '+number(retry)+' s':'waiting for retry confirmation'} · saved cursor retained`:
      phase==='idle'?'No job selected.':phase==='downloading'?'Waiting for the next worker update.':(descriptions[phase]||[phase])[0];
    if(running&&sameJob) {
      // The first snapshot is historical state. Only later deltas can trigger motion.
      const actions=(state.actions||[]).filter(action=>action.sequence>seen.sequence);
      if(actions.some(a=>a.kind==='request'&&!a.lookup_only))pulse('flow-request-packet',-60);
      if(actions.some(a=>a.kind==='response'))pulse('flow-response-packet',64);
      if(actions.some(a=>a.kind==='page_committed'))pulse('flow-archive-box');
      const added=Number(state.downloaded_events||0)-seen.downloaded;
      if(added>0) {
        activity=`+${number(added)} records saved since the last update`;
        pulse('flow-archive-box');
      } else if(actions.length)activity=describe(actions.at(-1))[0];
      // Older workers expose only counters. Do not imply an observed HTTP response.
      if(!state.actions?.length&&Number(state.requests||0)>seen.requests)pulse('flow-request-packet',-60);
      if(Number(state.reconstructed_events||0)>seen.reconstructed)pulse('flow-l3-box');
      if(Number(state.validated_events||0)>seen.validated)pulse('flow-verify-box');
    }
    $('flow-activity').textContent=activity;
  }
  const descriptions={
    idle:['No download is running','Start a bounded download to see the actual requests, returned pages, and saved cursors.'],
    starting:['Starting the worker','The server has started the existing download command. It checks the chain and fixed block range first.'],
    downloading:['Recording market logs with Envio','HyperSync filters logs by market address and event topics. Each page supplies matched logs, related block timestamps, and the next block cursor.'],
    rate_limit_wait:['Waiting for the request allowance','The cursor is saved. The worker waits for the server retry delay before it requests the same page again.'],
    checkpoint:['Loading the starting L3 checkpoint','RPC supplies the book before this partition. The worker restores orders, price levels, and FIFO queue links.'],
    contract_preflight:['Checking the market contract','The worker checks the contract implementation and supported state before reconstruction. Unsupported history stops the run.'],
    rpc_parity:['Comparing Envio events with RPC','The worker reads matching RPC logs in 500-block batches and compares them with the saved Envio events.'],
    reconstructing:['Applying events in historical order','Order and Trade events update the verified L3 book in block, transaction, and log order. No strategy is simulated.'],
    validating:['Checking reconstructed book state','The worker checks FIFO invariants and compares aggregated price levels with getL2Book() at distributed blocks.'],
    partition_validated:['Partition checks passed','Event parity, L2 checks, queue checks, and the final RPC checkpoint passed. The partition can be promoted by the existing pipeline.'],
    complete:['Job complete','All requested partitions have finished. A download-only job is still unvalidated.'],
    failed:['Job failed','The worker stopped after an error. Its saved cursor remains available for resume.'],
    stopped:['Job stopped','Committed pages remain on disk. Resume the same range to continue from its saved cursor.']
  };
  function node(tag,text,cls) {const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(cls)item.className=cls;return item;}
  function candlesChart(candles) {
    const target=$('chart');target.replaceChildren();if(!candles.length)return;
    const ns='http://www.w3.org/2000/svg',width=Math.max(240,target.clientWidth),height=140;
    function svgNode(tag,attributes,text) {const item=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attributes))item.setAttribute(key,value);if(text!==undefined)item.textContent=text;return item;}
    const svg=svgNode('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':'Hourly OHLC from actual Trade prices. USDC per MON.'});
    const low=Math.min(...candles.map(c=>Number(c.low))),high=Math.max(...candles.map(c=>Number(c.high))),pad=Math.max((high-low)*.1,high*.0001);
    const y=price=>16+(high+pad-Number(price))/(high-low+2*pad)*88,step=(width-72)/Math.max(4,candles.length);
    svg.append(svgNode('text',{x:0,y:12},'USDC / MON'));
    for(const price of [...new Set([low,high])])svg.append(svgNode('text',{x:0,y:y(price)+4},price.toFixed(6)));
    candles.forEach((c,index)=>{
      const x=72+(index+.5)*step,group=svgNode('g',{stroke:'#ff874a',fill:'#ff874a'});
      group.append(svgNode('line',{x1:x,x2:x,y1:y(c.high),y2:y(c.low)}));
      group.append(svgNode('rect',{x:x-Math.min(5,step*.25),y:Math.min(y(c.open),y(c.close)),width:Math.min(10,step*.5),height:Math.max(1,Math.abs(y(c.open)-y(c.close))),opacity:Number(c.close)>=Number(c.open)?1:.5}));
      group.append(svgNode('title',{},`${new Date(c.timestamp*1000).toISOString()} · O ${c.open} H ${c.high} L ${c.low} C ${c.close} · ${c.volume_mon} MON`));svg.append(group);
    });
    svg.append(svgNode('text',{x:72,y:130},new Date(candles[0].timestamp*1000).toISOString().slice(0,16)+' UTC'));target.append(svg);
  }
  function describe(action) {
    if(action.kind==='request')return [action.lookup_only?'Request missing block metadata':'Request a HyperSync page',`Blocks ${number(action.from_block)}–${number(Number(action.to_block_exclusive)-1)}${action.lookup_only?' · narrow timestamp lookup':' · market address + allowed event topics'}`];
    if(action.kind==='response')return ['Receive the response',`${number(action.log_count)} logs · ${number(action.block_count)} related blocks · ${number(action.decoded_response_bytes)} decoded JSON bytes · returned cursor ${action.next_block??'not supplied'}`];
    if(action.kind==='page_committed')return ['Commit the page and cursor',`${number(action.new_events)} new records of ${number(action.page_events)} returned · ${number(action.page_events-action.new_events)} duplicates skipped · next block ${number(action.next_block)} · ${number(action.committed_bytes)} saved bytes in this partition`];
    if(action.kind==='rate_limit_wait')return ['HTTP 429 · wait before retry',`${Number(action.seconds).toFixed(2)} seconds · cursor unchanged · ${JSON.stringify(action.headers||{})}`];
    if(action.kind==='l2_check')return ['L2 and FIFO checks passed',`Block ${number(action.block)} · checkpoint ${action.checks}/7 · price error ${action.price_level_error_raw} raw · size error ${action.aggregate_size_error_raw} raw`];
    return descriptions[action.phase]||['Process update',action.phase||action.kind];
  }
  function render(state) {
    const phase=state.phase||'idle',config=state.configuration,stale=config&&Date.now()/1000-state.updated_at>15+(phase==='rate_limit_wait'?Number(state.retry_seconds||0):0)&&!['complete','failed','stopped'].includes(phase);
    const [title,explanation]=descriptions[phase]||['Process update',phase];
    const lastAction=(state.actions||[]).at(-1);
    $('phase').textContent=stale?'No recent update':title;
    $('freshness').textContent=config?`Last worker update ${number(Math.floor(Math.max(0,Date.now()/1000-Number(state.updated_at||0))))} s ago`:'Idle';
    $('status-dot').style.background=['idle','failed','stopped'].includes(phase)||stale?'#69615b':'#ff874a';
    $('error').textContent=state.error||'';$('error').hidden=!state.error;
    $('current-action').textContent=stale?'No recent worker update':phase==='downloading'&&lastAction&&lastAction.kind!=='phase'?describe(lastAction)[0]:title;
    $('action-explanation').textContent=stale?'The last snapshot is shown below. This page does not assume that the worker is still active.':phase==='complete'&&config?.download_only?'All requested market logs are saved locally. L3 reconstruction, RPC parity, and candle export have not run.':explanation;
    if(state.managed_by_this_server===false)$('action-explanation').textContent+=' Showing the latest saved job. This server does not own its worker.';
    const job=JSON.stringify(config);
    renderFlow(state,stale,job);
    const signature=JSON.stringify([state,Boolean(stale)]);
    if(signature===rendered)return; // Keep timers live without rebuilding unchanged tables and charts.
    rendered=signature;
    $('scope').textContent=config?`${number(config.from_block)} — ${number(config.to_block)}`:'—';
    for(const key of ['downloaded','reconstructed','validated'])$(key).textContent=number(state[key+'_events']);
    const stages=$('process-stages');stages.replaceChildren();
    for(const [name,value,text,active] of [['01 · Request',number(state.requests),'HyperSync HTTP calls',phase==='downloading'&&lastAction?.kind==='request'],['02 · Receive',state.last_response?number(state.last_response.log_count):'—','Logs in the returned page',phase==='downloading'&&lastAction?.kind==='response'],['03 · Archive',number(state.downloaded_events),'Canonical records saved',phase==='downloading'&&lastAction?.kind==='page_committed'],['04 · Reconstruct',number(state.reconstructed_events),config?.download_only?'Not run · download only':'Historical events applied',['checkpoint','reconstructing'].includes(phase)],['05 · Verify',number(state.validated_events),config?.download_only?'Not run · download only':'Events in passing partitions',['rpc_parity','validating'].includes(phase)]]) {
      const card=node('div',undefined,'process-stage'+(active&&!stale?' active':''));card.append(node('span',name),node('strong',value),node('small',text));stages.append(card);
    }
    const inspector=$('page-inspector');inspector.replaceChildren();
    const request=state.current_request, response=state.last_response, commit=state.last_committed_page;
    for(const [label,value] of [['Requested cursor',request?number(request.from_block):'—'],['Returned cursor',response?.next_block??'—'],['Committed next block',commit?number(commit.next_block):state.pages>0?number(Number(state.downloaded_block)+1):'—'],['Partition',state.current_partition?.map(number).join('–')||'—']]) {
      const item=node('div');item.append(node('span',label),node('strong',String(value)));inspector.append(item);
    }
    const actions=state.actions||[],trace=$('action-trace');trace.replaceChildren();
    for(const action of [...actions].reverse()) {
      const item=node('li',undefined,seen&&seen.job===job&&action.sequence>seen.sequence?'trace-item fresh':'trace-item');
      const [label,detail]=describe(action), time=node('time',new Date(action.timestamp*1000).toISOString().slice(11,23));
      const content=node('div');content.append(node('strong',label),node('p',detail));
      if(action.event_counts)content.append(node('p',Object.entries(action.event_counts).map(([name,count])=>`${name} ${number(count)}`).join(' · '),'trace-event-counts'));
      item.append(time,content,node('span',`#${action.sequence}`,'trace-sequence'));trace.append(item);
    }
    if(!actions.length)trace.append(node('li',config?'This worker has no action trace. New workers report page-level actions.':'No actions yet. Nothing is moving or processing.','trace-empty'));
    seen={job,sequence:state.action_sequence||0,downloaded:Number(state.downloaded_events||0),requests:Number(state.requests||0),reconstructed:Number(state.reconstructed_events||0),validated:Number(state.validated_events||0)};
    const coverage=$('coverage');coverage.replaceChildren();
    if(config)for(const [label,key] of [['Downloaded','downloaded_block'],['Reconstructed','reconstructed_block'],['Validated','validated_block']]) {
      const percent=Math.max(0,Math.min(100,((state[key]??config.from_block-1)-config.from_block+1)/(config.to_block-config.from_block+1)*100));
      const row=node('div',undefined,'coverage-row'),track=node('div',undefined,'track'),bar=node('div',undefined,'bar');bar.style.width=percent+'%';track.append(bar);row.append(node('span',label),track,node('span',percent.toFixed(1)+'%','percent'));coverage.append(row);
    }
    const counts=commit?.event_counts||state.current_event_counts||state.event_counts||{},events=$('events');events.replaceChildren();
    $('event-count-heading').textContent=commit?'EVENTS IN LAST SAVED PAGE':'RECONSTRUCTION EVENT COUNTS';
    const maxCount=Math.max(1,...Object.values(counts).map(Number));
    for(const [name,count] of Object.entries(counts)) {const row=node('div',undefined,'event event-distribution'),track=node('div',undefined,'event-track'),bar=node('div',undefined,'event-bar');bar.style.width=(Number(count)/maxCount*100)+'%';track.append(bar);row.append(node('span',name),track,node('strong',number(count)));events.append(row);}
    if(!Object.keys(counts).length)events.textContent='No event counts reported yet';
    const details=$('details');details.replaceChildren();
    for(const [label,value] of [['Market',config?.market||'—'],['Mode',config?config.download_only?'Download only':'Reconstruction':'No job'],['Requests',number(state.requests)],['Pages',number(state.pages)],['Saved bytes',number(state.stored_bytes)],['Open orders',state.open_orders??'Not checked'],['Price levels',state.price_levels??'Not checked'],['Event parity',state.event_parity_status||'Not checked'],['Queue failures',state.queue_invariant_failures??'Not checked'],['Rate-limit waits',number(state.rate_limit_waits)]]) {const row=node('div',undefined,'detail');row.append(node('span',label),node('span',String(value)));details.append(row);}
    const chart=$('chart');chart.replaceChildren();
    if(config?.download_only)chart.append(node('p','Candle export is deferred. Downloads save raw Trade events, not candles.','suite-note'));
    else candlesChart(state.recent_candles||[]);
  }
  async function poll() {
    try {const response=await fetch('/api/indexing-progress',{cache:'no-store',signal:AbortSignal.timeout(5000)});if(!response.ok)throw new Error(`Status request failed: HTTP ${response.status}`);render(await response.json());}
    catch(error) {stopPulses();seen=null;rendered=null;$('flow-activity').textContent='Connection lost. Motion is paused.';$('phase').textContent='Disconnected';$('error').textContent=error.message;$('error').hidden=false;}
    setTimeout(poll,document.hidden?5000:1000);
  }
  poll();
})();
