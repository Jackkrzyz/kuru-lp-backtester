/* Real telemetry only. Draw bounded visual batches, never a synthetic stream. */
(() => {
  const $ = id => document.getElementById(id);
  const number = value => Number(value || 0).toLocaleString();
  const canvas = $('flow'), context = canvas.getContext('2d');
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let last = null, particles = [], animation = null, width = 0, height = 0, nodes = [], edges = [], connectionLost = false;
  const orange = '#ff874a', muted = '#8c8580';
  const stageNames = ['Monad', 'HyperSync', 'Archive', 'L3', 'Verified', 'Candles', 'RPC'];

  function layout() {
    if(animation!==null)cancelAnimationFrame(animation);
    width = canvas.clientWidth; height = canvas.clientHeight;
    const ratio = devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (width > 650) {
      nodes = [[.10,.44],[.29,.44],[.48,.44],[.68,.44],[.88,.44],[.68,.84],[.88,.08]].map(([x,y])=>[x*width,y*height]);
    } else {
      nodes = [[.20,.12],[.50,.12],[.80,.12],[.50,.47],[.50,.82],[.80,.47],[.20,.82]].map(([x,y])=>[x*width,y*height]);
    }
    edges = [[0,1],[1,2],[2,3],[3,4],[2,5],[6,4]];
    draw(performance.now());
  }

  function point(edge, t) {
    const [a,b] = edges[edge], start=nodes[a], end=nodes[b];
    return [start[0]+(end[0]-start[0])*t,start[1]+(end[1]-start[1])*t];
  }

  function draw(time) {
    animation = null;
    context.clearRect(0,0,width,height);
    context.fillStyle='#242122';
    for(let x=18;x<width;x+=24) for(let y=12;y<height;y+=24) {context.beginPath();context.arc(x,y,.6,0,Math.PI*2);context.fill();}
    context.lineWidth=1;
    edges.forEach((_,index)=> {
      const start=point(index,0), end=point(index,1);
      context.strokeStyle=index===5?'#453125':'#363030';
      context.beginPath();context.moveTo(...start);context.lineTo(...end);context.stroke();
    });
    const active={downloading:1,rate_limit_wait:1,checkpoint:3,reconstructing:3,rpc_parity:6,contract_preflight:6,validating:4,partition_validated:4,complete:4}[last?.phase];
    const paused=last && (!['complete','failed','stopped','idle'].includes(last.phase) && Date.now()/1000-last.updated_at>15);
    nodes.forEach(([x,y],index)=> {
      const selected=index===active&&!paused&&!connectionLost;
      if(selected) {
        const glow=context.createRadialGradient(x,y,12,x,y,42);glow.addColorStop(0,'#ff874a22');glow.addColorStop(1,'#ff874a00');
        context.fillStyle=glow;context.beginPath();context.arc(x,y,42,0,Math.PI*2);context.fill();
      }
      context.fillStyle='#141313';context.strokeStyle=selected?orange:'#4a403b';context.lineWidth=1;
      context.beginPath();context.roundRect(x-21,y-21,42,42,11);context.fill();context.stroke();
      context.strokeStyle=selected?orange:'#b5a69c';context.lineWidth=1.3;
      if(index===3) {
        for(let i=0;i<3;i++) {context.beginPath();context.moveTo(x-9,y-7+i*7);context.lineTo(x+9,y-7+i*7);context.stroke();}
      } else if(index===4) {
        context.beginPath();context.moveTo(x-8,y);context.lineTo(x-2,y+6);context.lineTo(x+9,y-7);context.stroke();
      } else if(index===5) {
        for(let i=0;i<3;i++) {context.beginPath();context.moveTo(x-7+i*7,y-11+i*2);context.lineTo(x-7+i*7,y+9-i*2);context.stroke();context.strokeRect(x-9+i*7,y-5+i*2,4,8);}
      } else {
        for(let i=0;i<3;i++) {context.beginPath();context.arc(x-7+i*7,y+(i%2?5:-4),2,0,Math.PI*2);context.stroke();}
      }
      context.fillStyle=selected?'#eee5de':muted;context.font='12px Arial';context.textAlign='center';context.fillText(stageNames[index],x,y+40);
    });
    particles=particles.filter(p=>time-p.started<1050);
    if(!paused && !reducedMotion.matches) particles.forEach(p=> {
      const t=Math.max(0,Math.min(1,(time-p.started)/950));
      const [x,y]=point(p.edge,t);
      context.shadowColor=orange;context.shadowBlur=9;context.fillStyle=orange;
      context.beginPath();context.arc(x,y,2,0,Math.PI*2);context.fill();context.shadowBlur=0;
    });
    if(particles.length&&!reducedMotion.matches&&!paused) animation=requestAnimationFrame(draw);
  }

  function emit(edge, amount) {
    if(amount<=0||reducedMotion.matches) return;
    const count=Math.min(8,Math.max(1,Math.ceil(Math.log10(amount+1)*2)));
    for(let i=0;i<count;i++) particles.push({edge,started:performance.now()+i*40});
    particles=particles.slice(-64);
  }

  function progressRows(state,c) {
    const container=$('coverage');container.replaceChildren();
    for(const [label,key] of [['Downloaded','downloaded_block'],['Reconstructed','reconstructed_block'],['Validated','validated_block']]) {
      const percentage=Math.max(0,Math.min(100,((state[key]??c.from_block-1)-c.from_block+1)/(c.to_block-c.from_block+1)*100));
      const row=document.createElement('div');row.className='coverage-row';
      const name=document.createElement('span');name.textContent=label;
      const track=document.createElement('div'),bar=document.createElement('div'),value=document.createElement('span');
      track.className='track';bar.className='bar';value.className='percent';value.textContent=percentage.toFixed(1)+'%';bar.style.width=percentage+'%';
      track.setAttribute('role','progressbar');track.setAttribute('aria-label',label);track.setAttribute('aria-valuenow',percentage);track.setAttribute('aria-valuemin',0);track.setAttribute('aria-valuemax',100);
      track.append(bar);row.append(name,track,value);container.append(row);
    }
  }

  function chart(candles) {
    const target=$('chart');target.replaceChildren();
    if(!candles.length) return;
    const ns='http://www.w3.org/2000/svg', w=Math.max(240,target.clientWidth), h=140;
    function element(tag,attrs,text) {const node=document.createElementNS(ns,tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);if(text!==undefined)node.textContent=text;return node;}
    const svg=element('svg',{viewBox:`0 0 ${w} ${h}`,role:'img','aria-label':'Hourly OHLC from actual Trade prices. USDC per MON.'});
    const low=Math.min(...candles.map(c=>Number(c.low))), high=Math.max(...candles.map(c=>Number(c.high))),pad=Math.max((high-low)*.1,high*.0001);
    const y=p=>16+(high+pad-Number(p))/(high-low+2*pad)*88;
    const step=(w-72)/Math.max(4,candles.length),left=72;
    svg.append(element('text',{x:0,y:12},'USDC / MON'));
    for(const price of [...new Set([low,high])]) svg.append(element('text',{x:0,y:y(price)+4},price.toFixed(6)));
    candles.forEach((c,index)=> {
      const x=left+(index+.5)*step,group=element('g',{stroke:orange,fill:orange});
      group.append(element('line',{x1:x,x2:x,y1:y(c.high),y2:y(c.low)}));
      group.append(element('rect',{x:x-Math.min(5,step*.25),y:Math.min(y(c.open),y(c.close)),width:Math.min(10,step*.5),height:Math.max(1,Math.abs(y(c.open)-y(c.close))),opacity:Number(c.close)>=Number(c.open)?1:.5}));
      group.append(element('title',{},`${new Date(c.timestamp*1000).toISOString()} · O ${c.open} H ${c.high} L ${c.low} C ${c.close} · ${c.volume_mon} MON`));svg.append(group);
    });
    svg.append(element('text',{x:left,y:130},new Date(candles[0].timestamp*1000).toISOString().slice(0,16)+' UTC'));
    target.append(svg);
  }

  function render(state) {
    connectionLost=false;
    const phase=state.phase||'idle', c=state.configuration;
    const stale=c&&Date.now()/1000-state.updated_at>15&&!['complete','failed','stopped'].includes(phase);
    const labels={idle:'Awaiting job',downloading:'Recording',rate_limit_wait:'Rate limited',checkpoint:'Loading checkpoint',reconstructing:'Reconstructing',rpc_parity:'Checking events',contract_preflight:'Checking contract',validating:'Validating',partition_validated:'Partition verified',complete:'Complete',failed:'Stopped',stopped:'Stopped'};
    $('phase').textContent=stale?'No recent update':labels[phase]||phase;
    $('freshness').textContent=c?`${number(Math.floor(state.elapsed_seconds))} s`:'';
    $('error').textContent=state.error||'';$('error').hidden=!state.error;
    $('status-dot').style.background=stale||['failed','stopped','idle'].includes(phase)?'#69615b':orange;
    if(c) {
      $('scope').textContent=`${number(c.from_block)} — ${number(c.to_block)}`;
      ['downloaded','reconstructed','validated'].forEach(key=>$(key).textContent=number(state[key+'_events']));
      progressRows(state,c);
    } else {
      ['downloaded','reconstructed','validated'].forEach(key=>$(key).textContent='0');
      $('scope').textContent='—';$('coverage').replaceChildren();
    }
    const events=$('events');events.replaceChildren();
    const counts={...(state.event_counts||{})};
    if(!['complete','partition_validated'].includes(phase)) for(const [name,count] of Object.entries(state.current_event_counts||{}))counts[name]=(counts[name]||0)+count;
    for(const [name,count] of Object.entries(counts)) {const row=document.createElement('div'),label=document.createElement('span'),value=document.createElement('strong');row.className='event';label.textContent=name;value.textContent=number(count);row.append(label,value);events.append(row);}
    if(!Object.keys(counts).length)events.textContent='—';
    const details=$('details');details.replaceChildren();
    for(const [label,value] of [['Market',c?.market||'—'],['Mode',c?.download_only?'Archive only':'Reconstruction'],['Requests',number(state.requests)],['Pages',number(state.pages)],['Saved bytes',number(state.stored_bytes)],['Open orders',number(state.open_orders)],['Price levels',number(state.price_levels)],['Event parity',state.event_parity_status||'Pending'],['Queue failures',state.queue_invariant_failures??'Pending'],['Rate-limit waits',number(state.rate_limit_waits)],['Rate-limit headers',JSON.stringify(state.rate_limit_headers||{})]]) {const row=document.createElement('div'),name=document.createElement('span'),data=document.createElement('span');row.className='detail';name.textContent=label;data.textContent=value;row.append(name,data);details.append(row);}
    const sameJob=last&&JSON.stringify(last.configuration)===JSON.stringify(c);
    if(!sameJob)particles=[];
    if(sameJob&&last.updated_at!==state.updated_at&&!stale&&!['failed','stopped','rate_limit_wait'].includes(phase)) {
      const received=Number(state.downloaded_events||0)-Number(last.downloaded_events||0);
      emit(0,received);emit(1,received);
      emit(2,Number(state.reconstructed_events||0)-Number(last.reconstructed_events||0));
      emit(3,Number(state.validated_events||0)-Number(last.validated_events||0));
      emit(4,Math.max(0,(state.recent_candles||[]).length-(last.recent_candles||[]).length));
      emit(5,Number(state.rpc_events_current_partition||0)-Number(last.rpc_events_current_partition||0));
    }
    if(stale||['failed','stopped','rate_limit_wait'].includes(phase))particles=[];
    last=state;chart(state.recent_candles||[]);
    if(animation!==null)cancelAnimationFrame(animation);
    draw(performance.now());
  }

  async function poll() {
    try {
      const response=await fetch('/api/indexing-progress',{cache:'no-store',signal:AbortSignal.timeout(5000)});
      if(!response.ok)throw new Error(`Status request failed: HTTP ${response.status}`);
      render(await response.json());
    } catch(error) {
      connectionLost=true;
      particles=[];if(animation!==null)cancelAnimationFrame(animation);
      $('phase').textContent='Disconnected';$('error').textContent=error.message;$('error').hidden=false;draw(performance.now());
    }
    setTimeout(poll,1000);
  }
  new ResizeObserver(()=>{layout();if(last)chart(last.recent_candles||[]);}).observe(canvas);
  layout();poll();
})();
