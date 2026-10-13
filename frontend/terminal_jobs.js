/* Current worker banners, saved job history, and a technical activity trace. */
(function(root){
  const $=id=>document.getElementById(id), jobs=new Map(), entries=new Map(), expandedJobs=new Set();
  const number=n=>Number(n||0).toLocaleString();
  const panel=$('job-panel'),toggle=$('log-toggle'),body=$('log-body');
  let minimized=false;
  function setMinimized(value){
    minimized=value;
    panel.classList.toggle('is-minimized',value);
    document.body.classList.toggle('logs-minimized',value);
    body.hidden=value;
    toggle.textContent=value?'Expand':'Minimize';
    toggle.setAttribute('aria-expanded',String(!value));
    try{root.localStorage?.setItem('kurulab.logs.minimized',String(value));}catch{}
  }
  try{minimized=root.localStorage?.getItem('kurulab.logs.minimized')==='true';}catch{}
  setMinimized(minimized);
  toggle.addEventListener('click',()=>setMinimized(!minimized));
  $('jobs-open').addEventListener('click',()=>{$('jobs-dialog').showModal();render();});
  $('jobs-close').addEventListener('click',()=>$('jobs-dialog').close());
  document.querySelectorAll('a[href="#job-panel"]').forEach(link=>link.addEventListener('click',event=>{
    event.preventDefault();setMinimized(false);toggle.focus();
  }));
  function describe(a){
    const bounds=a.partition?' · blocks '+a.partition.join('–'):'';
    if(a.kind==='rpc_response')return `Archive RPC · ${Object.entries(a.methods).map(([name,count])=>`${name} × ${count}`).join(', ')} · ${a.seconds}s · ${a.phase}${bounds}`;
    if(a.kind==='rpc_retry')return `Archive RPC retry ${a.attempt} · ${a.http_status?'HTTP '+a.http_status:'transport error'} · wait ${a.seconds}s${bounds}`;
    if(a.kind==='partition_input')return `Local canonical logs · ${a.from_block}–${a.to_block} · market ${a.market} · ${number(a.events)} events · ${JSON.stringify(a.event_counts)}`;
    if(a.kind==='checkpoint_loaded')return `L3 checkpoint @ ${a.block} · ${a.reused?'reuse verified prior end state':'archive RPC capture'} · ${number(a.orders)} orders / ${number(a.levels)} levels · implementation SHA256 ${a.implementation} · checkpoint SHA256 ${a.sha256}`;
    if(a.kind==='rpc_parity_check')return `eth_getLogs parity · ${a.from_block}–${a.to_block} · ${number(a.rpc_events)} RPC logs · ${a.status} · missing ${a.missing_from_either_source}`;
    if(a.kind==='timestamp_check')return `eth_getBlockByNumber · ${number(a.checked)}/${number(a.total)} event-block timestamps read for comparison with HyperSync`;
    if(a.kind==='cancellation_audit')return `OrderCanceled / OrdersCanceled audit · ${JSON.stringify(a)}`;
    if(a.kind==='final_l3_match')return `Full L3 RPC comparison @ ${a.block} · ${number(a.orders)} orders / ${number(a.levels)} levels · previous/next links, heads/tails and sizes: ${a.status}`;
    if(a.kind==='verified_partition')return `Partition passed · ${a.from_block}–${a.to_block} · ${number(a.events)} events / ${number(a.trades)} Trades · ${a.l2_checks}/7 getL2Book checks · price/size error ${a.price_error_raw}/${a.size_error_raw} · ${a.candles} Trade OHLC buckets · source SHA256 ${a.source_sha256}`;
    if(a.kind==='backtest_readiness')return `LP eligibility · ${a.status} · ${a.reason||`${a.from_block}–${a.to_block}`}`;
    if(a.kind==='request')return `HyperSync request · blocks ${a.from_block}–${Number(a.to_block_exclusive)-1}${a.lookup_only?' · missing timestamp lookup':' · market + topic filters'}`;
    if(a.kind==='response')return `HyperSync response · ${number(a.log_count)} logs · ${number(a.block_count)} blocks · next cursor ${a.next_block??'not returned'}`;
    if(a.kind==='page_committed')return `Saved ${number(a.new_events)} events · ${number(a.page_events-a.new_events)} duplicates skipped · durable cursor ${a.next_block}`;
    if(a.kind==='rate_limit_wait')return `HTTP 429 · wait ${a.seconds}s · saved cursor retained · ${JSON.stringify(a.headers||{})}`;
    if(a.kind==='l2_check')return `L2 check ${a.checks}/7 · block ${a.block} · price error ${a.price_level_error_raw} · size error ${a.aggregate_size_error_raw} · queue ${a.queue_invariants}`;
    const phases={checkpoint:'Capture L3 state before the first block; read contract implementation and queue storage',
      rpc_parity:'Compare local HyperSync logs with archive eth_getLogs in 500-block windows',
      rpc_timestamps:'Check event-block timestamps against archive eth_getBlockByNumber',
      reconstructing:'Replay canonical events in block / transaction / log order; update FIFO queues',
      validating:'Compare aggregate levels with getL2Book; check queue links and contract runtime',
      final_l3_checkpoint:'Read complete ending L3 queues from archive RPC; compare every order and queue link',
      copying_downloaded_partition:'Copy completed partition; check cursor, byte count, event count and SHA256',
      partition_validated:'All partition checks passed; commit validation report and Trade candles'};
    return (phases[a.phase]||String(a.phase||a.kind).replaceAll('_',' '))+bounds;
  }
  function ingest(id,type,phase,progress,running=false,trace=true){
    const before=jobs.get(id);jobs.set(id,{id,type,phase,progress,running});
    if(!trace)return;
    for(const action of progress.actions||[])entries.set(`${id}:${action.timestamp}:${action.sequence}`,{id,type,time:action.timestamp,text:describe(action)});
    if(before?.phase!==phase||before?.progress?.error!==progress.error)entries.set(`${id}:state:${phase}:${progress.updated_at||0}`,{id,type,time:progress.updated_at||Date.now()/1000,text:describe({phase,partition:progress.current_partition})+(progress.error?' · '+progress.error:'')});
    const observed={rpc_checked_block:'RPC parity checked through block',timestamp_blocks_checked:'RPC event-block timestamps checked',
      reconstructed_block:'FIFO replay processed through block',validated_block:'Verified archive committed through block',processed_files:'Local Trade files indexed'};
    for(const [key,label] of Object.entries(observed))if(progress[key]!=null&&progress[key]!==before?.progress?.[key])
      entries.set(`${id}:${key}:${progress[key]}`,{id,type,time:progress.updated_at||Date.now()/1000,
        text:`${label} ${number(progress[key])}${key==='timestamp_blocks_checked'?' / '+number(progress.timestamp_blocks_total):''}${progress.current_partition?' · partition '+progress.current_partition.join('–'):''}`});
    if(entries.size>500){const ordered=[...entries].sort((a,b)=>a[1].time-b[1].time);for(const [key] of ordered.slice(0,entries.size-500))entries.delete(key);}
  }
  const titles={download:'HyperSync download',verification:'Reconstruct / verify',preparation:'Prepare market data',backtest:'LP backtest',chart:'Trade candle index'};
  function progressPercent(job){
    const p=job.progress;
    if(['complete','completed'].includes(job.phase))return 100;
    let value=null;
    if(job.type==='backtest'&&p.percent_complete!=null)value=Number(p.percent_complete);
    else if(p.total_partitions>0)value=100*Number(p.processed_partitions??p.completed_partitions??0)/p.total_partitions;
    else if(job.type==='chart'&&p.total_files>0)value=100*Number(p.processed_files||0)/p.total_files;
    else if(['download','verification'].includes(job.type)){
      const first=Number(p.configuration?.from_block),last=Number(p.configuration?.to_block);
      const cursor=job.type==='download'?p.downloaded_block:p.validated_block;
      if(Number.isFinite(first)&&Number.isFinite(last)&&last>=first)value=cursor==null?0:100*(Number(cursor)-first+1)/(last-first+1);
    }
    return value!=null&&Number.isFinite(value)?Math.min(100,Math.max(0,value)):null;
  }
  function meter(job){
    const percent=progressPercent(job),bar=document.createElement('div'),fill=document.createElement('i');
    bar.className='job-meter'+(percent==null&&job.running?' indeterminate':'');
    bar.setAttribute('role','progressbar');bar.setAttribute('aria-label',(titles[job.type]||job.type)+' progress');
    bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');
    if(percent!=null){bar.setAttribute('aria-valuenow',percent.toFixed(1));fill.style.width=percent+'%';}
    else bar.setAttribute('aria-valuetext','Work in progress; total is not yet known');
    bar.append(fill);return bar;
  }
  function heading(job){
    const line=document.createElement('span');line.className='job-status-line';
    const title=document.createElement('strong'),status=document.createElement('span');
    const percent=progressPercent(job);
    const phase=job.phase==='unmanaged'?(job.progress.phase||'unmanaged'):job.phase.split(' · ').at(-1);
    const labels={checkpoint:'Loading checkpoint',rpc_parity:'Checking events',rpc_timestamps:'Checking timestamps',
      reconstructing:'Reconstructing',validating:'Checking order book',final_l3_checkpoint:'Checking final queues',
      copying_downloaded_partition:'Reading saved data',partition_validated:'Partition verified',
      indexing_local_trades:'Building candles',finding_local_files:'Finding saved data',
      downloading:'Downloading',rate_limit_wait:'Waiting for rate limit',verifying:'Verifying',planning:'Preparing'};
    title.textContent=titles[job.type]||job.type;
    status.textContent=(job.phase.startsWith('unmanaged')?'External · ':'')+(labels[phase]||phase.replaceAll('_',' '))+
      (percent==null?'':` · ${percent.toFixed(1)}%`);
    line.append(title,status);
    if(job.running||recentExternal(job)){const dots=document.createElement('span');dots.className='status-dots';dots.textContent='...';dots.setAttribute('aria-hidden','true');line.append(dots);}
    return line;
  }
  function recentExternal(job){
    return job.phase.startsWith('unmanaged')&&job.progress.updated_at>Date.now()/1000-120&&
      !['complete','completed','failed','stopped'].includes(job.progress.phase);
  }
  function details(job){
    const p=job.progress,parts=[`ID ${job.id}`,`market ${p.configuration?.market||'MON / USDC'}`];
    if(recentExternal(job))parts.push('Recent external worker activity; not managed by this server');
    const first=p.configuration?.from_block??p.current_from_block,last=p.configuration?.to_block??p.current_to_block;
    if(first!=null)parts.push(`range ${number(first)}–${number(last)}`);
    if(p.current_partition)parts.push(`partition ${p.current_partition.map(number).join('–')}`);
    if(p.elapsed_seconds!=null)parts.push(`elapsed ${(p.elapsed_seconds/3600).toFixed(2)}h`);
    if(p.updated_at)parts.push(`updated ${Math.floor(Math.max(0,Date.now()/1000-p.updated_at))}s ago`);
    if(p.total_partitions!=null)parts.push(`${number(p.completed_partitions)} verified / ${number(p.total_partitions)} partitions · ${number(p.skipped_verified)} already verified · ${number(p.blocked_partitions)} unsupported`);
    if(job.type==='verification'){
      if(p.reconstructed_events!=null)parts.push(`${number(p.reconstructed_events)} replayed / ${number(p.validated_events)} verified events`);
      if(p.l2_checks_current_partition!=null)parts.push(`${number(p.l2_checks_current_partition)}/7 getL2Book checks`);
      if(p.checkpoint_reused!=null)parts.push(p.checkpoint_reused?'checkpoint reused':'RPC checkpoint capture');
      if(p.timestamp_blocks_total!=null)parts.push(`timestamp checks ${number(p.timestamp_blocks_checked)}/${number(p.timestamp_blocks_total)}`);
      if(p.rpc_checked_block)parts.push(`RPC parity through ${number(p.rpc_checked_block)}`);
      if(p.rpc_http_responses!=null)parts.push(`${number(p.rpc_http_responses)} RPC responses · ${number(p.rpc_retry_count)} retries`);
      if(p.open_orders!=null)parts.push(`${number(p.open_orders)} open orders / ${number(p.price_levels)} levels`);
    }
    if(p.validated_events>0&&p.validated_block)parts.push(`verified through ${number(p.validated_block)}`);
    if(job.type==='download')parts.push(`${number(p.downloaded_events)} events saved · ${number(p.requests)} HyperSync requests · ${number(p.rate_limit_waits)} rate limits`);
    if(job.type==='chart')parts.push(`${number(p.processed_files)} files · ${p.current_range?.join('–')||''} · ${number(p.candles)} minute candles`);
    if(job.type==='backtest')parts.push(`${number(p.processed_events)} events processed`);
    if(p.rpc_wait_until>Date.now()/1000)parts.push(`RPC retry in ${Math.ceil(p.rpc_wait_until-Date.now()/1000)}s`);
    if(p.backtest_readiness?.status==='reconstruction_only')parts.push(`Candles available; LP unavailable: ${p.backtest_readiness.reason}`);
    if(p.warning)parts.push(p.warning);if(p.error)parts.push(p.error);
    return parts.join(' · ');
  }
  function brief(job){
    const p=job.progress,parts=[];
    if(job.type==='verification'&&p.total_partitions>0)
      parts.push(`${number(p.completed_partitions)} / ${number(p.total_partitions)} partitions verified`);
    else if(job.type==='verification'&&p.timestamp_blocks_total>0)
      parts.push(`${number(p.timestamp_blocks_checked)} / ${number(p.timestamp_blocks_total)} timestamps`);
    else if(job.type==='download')parts.push(`${number(p.downloaded_events)} events saved`);
    else if(job.type==='chart')parts.push(`${number(p.processed_files)} files read`);
    else if(job.type==='backtest')parts.push(`${number(p.processed_events)} events processed`);
    const range=p.current_partition||p.current_range;
    if(range)parts.push(`Blocks ${range.map(number).join('–')}`);
    if(p.rpc_wait_until>Date.now()/1000)parts.push(`Retry in ${Math.ceil(p.rpc_wait_until-Date.now()/1000)}s`);
    if(p.error)parts.push('See Details for the error');
    return parts.join(' · ');
  }
  function disclosure(job){
    const box=document.createElement('details'),label=document.createElement('summary'),text=document.createElement('div');
    box.className='job-disclosure';box.open=expandedJobs.has(job.id);
    label.textContent='Details';text.className='job-details';text.textContent=details(job);
    box.append(label,text);
    box.addEventListener('toggle',()=>{if(box.open)expandedJobs.add(job.id);else expandedJobs.delete(job.id);});
    return box;
  }
  function render(){
    const all=[...jobs.values()],active=all.filter(j=>(j.running||recentExternal(j))&&!j.progress.parent_job_id);
    $('job-headline').textContent=active.length?`${active.length} ${active.some(recentExternal)?'current':'active'} ${active.length===1?'job':'jobs'}`:'No managed job running';
    const detail=$('job-detail');detail.replaceChildren();
    for(const job of active){
      const node=document.createElement('section'),text=document.createElement('div');node.className='banner-job';
      text.className='job-brief';text.textContent=brief(job);
      node.append(heading(job),meter(job),text,disclosure(job));detail.append(node);
    }
    const summaries=$('job-summary');summaries.replaceChildren();
    for(const job of [...all].sort((a,b)=>(b.progress.updated_at||0)-(a.progress.updated_at||0))){
      const row=document.createElement('div');row.className='job-summary-row';
      const text=document.createElement('span');text.className='job-brief';text.textContent=brief(job);
      row.replaceChildren();row.append(heading(job),text,meter(job),disclosure(job));
      summaries.append(row);
    }
    const filter=$('log-filter').value,trace=$('activity-log');trace.replaceChildren();
    for(const entry of [...entries.values()].filter(e=>filter==='all'||e.type===filter||(filter==='verification'&&e.type==='preparation')).sort((a,b)=>b.time-a.time).slice(0,120)){
      const row=document.createElement('li'),time=document.createElement('time'),type=document.createElement('span'),message=document.createElement('span');
      time.textContent=new Date(entry.time*1000).toISOString().replace('T',' ').slice(0,19);type.textContent=entry.type;message.textContent=entry.text;
      row.title=entry.id;row.append(time,type,message);trace.append(row);
    }
  }
  root.LabJobs={
    update(value){
      // Each suite snapshot is authoritative for worker ownership. Removed
      // jobs must not remain marked as running in this page's retained log.
      for(const job of jobs.values())if(['download','verification','preparation'].includes(job.type))job.running=false;
      if(value.observed_progress?.configuration)ingest('external','download','unmanaged',value.observed_progress,false);
      for(const job of value.jobs||[])ingest(job.job_id,'download',job.phase,{...job.progress,configuration:job.request},job.running);
      for(const job of value.job_history||[])if(job.job_id!==value.verification_queue?.job_id&&job.job_id!==value.pipeline?.job_id&&!jobs.get(job.job_id)?.running)
        ingest(job.job_id,job.type,job.phase,{...job.progress,configuration:job.request},false,false);
      for(const job of value.verifications||[])ingest(job.job_id,'verification',job.phase,{...job.progress,configuration:job.request,
        parent_job_id:value.verification_queue?.progress?.current_job_id===job.job_id?value.verification_queue.job_id:null},job.running);
      const queue=value.verification_queue;if(queue){
        const worker=queue.current_progress||{};
        const phase=['verifying','unmanaged'].includes(queue.phase)&&worker.phase?queue.phase+' · '+worker.phase:queue.phase;
        ingest(queue.job_id,'verification',phase,{...worker,...queue.progress,actions:[],
          updated_at:Math.max(queue.progress.updated_at||0,worker.updated_at||0)},queue.running,!worker.phase);
      }
      const discovery=value.local_trade_discovery;if(discovery)ingest('archive-trades','chart',discovery.phase,discovery,['indexing_local_trades','finding_local_files'].includes(discovery.phase));
      const pipeline=value.pipeline;if(pipeline)ingest(pipeline.job_id,'preparation',pipeline.phase,pipeline,pipeline.running);render();},
    observed(p){if(p.configuration)ingest(p.observed_job_id||'download','download',p.managed_by_this_server===false?'unmanaged':p.phase,p,false);render();},
    backtest(id,p){ingest(id,'backtest',p.stage,{...p,updated_at:Date.now()/1000},!['failed','canceled','completed','stalled'].includes(p.status));render();},
    chart(p){ingest('local-candles','chart',p.phase,p,['indexing_local_trades','finding_local_files'].includes(p.phase));render();}
  };
  $('log-filter').addEventListener('change',render);
})(window);
