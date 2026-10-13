/* Chart polling is separate from strategy requests and never runs a backtest. */
(() => {
  window.LabChart=KuruChart.mount(document.getElementById('market-chart'));
  window.addEventListener('page-ready',()=>requestAnimationFrame(()=>LabChart.fit()));
  let payload={candles:[]},view=null,controller=null,timer;
  const element=document.getElementById('market-chart'),dock=document.getElementById('job-panel');
  let resizeFrame=0;
  function fitHeight(){
    if(resizeFrame)return;
    resizeFrame=requestAnimationFrame(()=>{
      resizeFrame=0;if(!element.clientWidth)return;
      const top=element.getBoundingClientRect().top+window.scrollY;
      const available=dock.getBoundingClientRect().top-top-12;
      const height=Math.max(120,Math.min(530,Math.floor(available)));
      if(element.style.getPropertyValue('--chart-height')!==height+'px')element.style.setProperty('--chart-height',height+'px');
    });
  }
  const layoutObserver=new ResizeObserver(fitHeight);
  for(const node of [dock,document.querySelector('.terminal-heading'),document.querySelector('.chart-toolbar'),document.querySelector('.pnl-key')])if(node)layoutObserver.observe(node);
  window.addEventListener('resize',fitHeight);window.addEventListener('page-ready',fitHeight);
  window.addEventListener('chart-interaction-start',()=>{controller?.abort();controller=null;clearTimeout(timer);});
  window.addEventListener('chart-interaction-end',()=>{clearTimeout(timer);timer=setTimeout(poll,700);});
  window.addEventListener('chart-window-changed',event=>{
    view=event.detail;controller?.abort();clearTimeout(timer);poll();
  });
  document.getElementById('chart-fit').addEventListener('click',()=>LabChart.fit());
  document.getElementById('candle-interval').addEventListener('change',()=>{view=LabChart.range;controller?.abort();clearTimeout(timer);poll();});
  window.addEventListener('local-coverage',event=>{
    if(Array.isArray(event.detail.backtest_ranges))window.LabBacktestCoverage=event.detail.backtest_ranges;
    LabChart.update(payload);
  });
  window.addEventListener('chart-range-selected',event=>{
    if(document.getElementById('backtest-form'))return;
    document.getElementById('download-from').value=event.detail.from_block;
    document.getElementById('download-to').value=event.detail.to_block;
    document.getElementById('download-from').dispatchEvent(new Event('input'));
  });
  async function poll(){
    const request=new AbortController();controller=request;
    const timeout=setTimeout(()=>request.abort(),10000);
    try{
      const padding=view?Math.max(60,(view.to-view.from)/2):0;
      const seconds=document.getElementById('candle-interval').value;
      const detailed=Boolean(view)&&Number(seconds)<86400;
      const query=`?seconds=${seconds}`+(detailed?`&from_time=${Math.floor(view.from-padding)}&to_time=${Math.ceil(view.to+padding)}`:'');
      const response=await fetch('/api/market-chart'+query,{cache:'no-store',signal:request.signal});
      if(!response.ok)throw new Error(`Local candle API: HTTP ${response.status}`);
      const next=await response.json();if(controller!==request)return;
      payload={...next,window:detailed};const changed=LabChart.update(payload);LabJobs.chart(payload.state);
      const duration=payload.candle_seconds>=604800?`${payload.candle_seconds/604800} week`:payload.candle_seconds>=86400?`${payload.candle_seconds/86400} day`:payload.candle_seconds>=3600?`${payload.candle_seconds/3600} hour`:`${payload.candle_seconds/60} minute`;
      document.getElementById('candle-status').textContent=`${payload.candles.length.toLocaleString()} candles · ${duration} bars · ${payload.saved_overview?'Saved overview · ':''}${payload.state.phase.replaceAll('_',' ')}${payload.state.error?' · '+payload.state.error:''} · Verified partitions · OHLC from Trade events. LP approval is checked separately.`;
      if(changed)window.dispatchEvent(new CustomEvent('market-candles',{detail:{...payload,candles:LabChart.bars}}));
    }catch(error){if(controller===request)document.getElementById('candle-status').textContent=error.message;}
    finally{clearTimeout(timeout);if(controller===request)timer=setTimeout(poll,document.hidden?15000:3000);}
  }
  poll();
})();
