/* Local Trade candles, stable navigation, and queue-aware replay overlays. */
(function(root) {
  const utc=time=>new Date(Number(time)*1000).toISOString().replace('T',' ').slice(0,19)+' UTC';
  function mount(element) {
    const L=LightweightCharts;
    const chart=L.createChart(element,{autoSize:true,layout:{background:{color:'#121416'},textColor:'#bcc3ca',fontSize:11,attributionLogo:true},
      grid:{vertLines:{color:'#ff874a09'},horzLines:{color:'#ff874a09'}},rightPriceScale:{borderColor:'#303438'},
      timeScale:{visible:true,minimumHeight:32,timeVisible:false,secondsVisible:false,borderColor:'#52575c',shiftVisibleRangeOnNewBar:false,rightBarStaysOnScroll:true,fixRightEdge:false,
        tickMarkFormatter:time=>utc(time).slice(0,10)},localization:{timeFormatter:utc},
      handleScroll:{pressedMouseMove:true,mouseWheel:true,horzTouchDrag:true,vertTouchDrag:false}});
    const candles=chart.addSeries(L.CandlestickSeries,{upColor:'#9faeaa',downColor:'#ff874a',wickUpColor:'#9faeaa',wickDownColor:'#ff874a',borderVisible:false,
      priceFormat:{type:'price',precision:6,minMove:.000001}});
    let equity=null,hold=null;
    const legend=element.previousElementSibling;
    const markers=L.createSeriesMarkers(candles,[]);
    const overlay=document.createElement('canvas');overlay.className='coverage-overlay';element.append(overlay);
    let bars=[],points=[],selected=null,drag=null,navigating=false,selecting=false,fitted=false,
        signature='',replayCandleSeconds=86400,paintFrame=0,zoomTimer,replayMemo=null,axisTimes=[];
    const lines=new Map();
    function lowerBound(items,value,key) {
      let lo=0,hi=items.length;
      while(lo<hi){const mid=(lo+hi)>>1;if(key(items[mid])<value)lo=mid+1;else hi=mid;}
      return lo;
    }
    function blockTime(block) {
      if(!points.length)return null;
      const i=lowerBound(points,block,p=>p.block);
      if(i===0)return points[0].time;if(i===points.length)return points.at(-1).time;
      const a=points[i-1],b=points[i];
      return b.block===a.block?a.time:a.time+(block-a.block)/(b.block-a.block)*(b.time-a.time);
    }
    function timeBar(time) {
      if(!bars.length)return null;
      const i=lowerBound(bars,time,b=>b.time);
      if(i===0)return bars[0];if(i===bars.length)return bars.at(-1);
      return time-bars[i-1].time<=bars[i].time-time?bars[i-1]:bars[i];
    }
    function coordinate(time) {
      const scale=chart.timeScale(),direct=scale.timeToCoordinate(Math.round(time));
      if(direct!==null)return direct;
      const range=scale.getVisibleRange();if(!range)return 0;
      const x1=scale.timeToCoordinate(range.from),x2=scale.timeToCoordinate(range.to);
      return x1+(time-range.from)/Math.max(1,range.to-range.from)*(x2-x1);
    }
    function paint() {
      paintFrame=0;
      const ratio=devicePixelRatio||1,w=element.clientWidth,h=element.clientHeight;
      if(overlay.width!==Math.round(w*ratio)||overlay.height!==Math.round(h*ratio)){
        overlay.width=Math.round(w*ratio);overlay.height=Math.round(h*ratio);
      }
      const ctx=overlay.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);
      if(!bars.length)return;
      const band=Math.max(0,chart.panes()[0].getHeight()-8);
      ctx.save();ctx.beginPath();ctx.rect(0,0,Math.max(0,w-65),band+8);ctx.clip();
      if(selected){
        const x1=coordinate(blockTime(selected[0])),x2=coordinate(blockTime(selected[1]));
        ctx.fillStyle='#ff874a18';ctx.fillRect(x1,0,Math.max(2,x2-x1),band);
        ctx.strokeStyle='#ff874a';ctx.strokeRect(x1+.5,.5,Math.max(1,x2-x1),band);
      }
      ctx.restore();
    }
    function requestPaint(){if(!paintFrame)paintFrame=requestAnimationFrame(paint);}
    function requestWindow(){
      clearTimeout(zoomTimer);
      zoomTimer=setTimeout(()=>{
        const range=chart.timeScale().getVisibleRange();
        if(range)root.dispatchEvent(new CustomEvent('chart-window-changed',{detail:range}));
      },500);
    }
    function manualInteraction(){fitted=true;root.dispatchEvent(new CustomEvent('chart-interaction-start'));}
    chart.timeScale().subscribeVisibleTimeRangeChange(requestPaint);
    chart.timeScale().subscribeVisibleLogicalRangeChange(requestPaint);
    function resizeReplayPane(){
      requestAnimationFrame(()=>{
        const pane=chart.panes()[1],height=Math.min(90,Math.floor(element.clientHeight/4));
        if(pane&&element.clientHeight>0&&pane.getHeight()!==height)pane.setHeight(height);
        requestPaint();
      });
    }
    new ResizeObserver(resizeReplayPane).observe(element);
    chart.subscribeCrosshairMove(param=>{
      const bar=param.seriesData.get(candles),label=document.getElementById('chart-crosshair');
      if(label&&bar?.close!==undefined)label.textContent=utc(param.time)+' · O '+bar.open.toFixed(6)+' H '+bar.high.toFixed(6)+' L '+bar.low.toFixed(6)+' C '+bar.close.toFixed(6);
    });
    element.addEventListener('pointerdown',event=>{
      if(event.button!==0||event.target.closest('a'))return;
      const rect=element.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
      if(x>element.clientWidth-65||y>chart.panes()[0].getHeight()-8)return;
      manualInteraction();
      if(!selecting){navigating=true;return;}
      const time=chart.timeScale().coordinateToTime(x);
      if(time===null)return;drag={time,x,moved:false};element.setPointerCapture(event.pointerId);
    });
    element.addEventListener('pointermove',event=>{
      if(!drag)return;
      const x=event.clientX-element.getBoundingClientRect().left;
      if(Math.abs(x-drag.x)<4&&!drag.moved)return;
      const time=chart.timeScale().coordinateToTime(x);if(time===null)return;
      drag.moved=true;
      const a=timeBar(Math.min(drag.time,time)),b=timeBar(Math.max(drag.time,time));
      if(a&&b){selected=[a.from_block,b.to_block];requestPaint();}
    });
    function endPointer(){
      if(navigating){navigating=false;requestWindow();}
      if(!drag)return;const moved=drag.moved;drag=null;
      if(moved&&selected)root.dispatchEvent(new CustomEvent('chart-range-selected',{detail:{from_block:selected[0],to_block:selected[1]}}));
      root.dispatchEvent(new CustomEvent('chart-interaction-end'));
    }
    element.addEventListener('pointerup',endPointer);
    root.addEventListener?.('pointerup',endPointer);
    element.addEventListener('pointercancel',()=>{drag=null;navigating=false;root.dispatchEvent(new CustomEvent('chart-interaction-end'));});
    element.addEventListener('wheel',()=>{manualInteraction();requestWindow();},{passive:true});
    function setMode(value){
      selecting=value==='select';
      chart.applyOptions({handleScroll:{pressedMouseMove:!selecting,horzTouchDrag:!selecting}});
      for(const [id,active] of [['chart-navigate',!selecting],['chart-select',selecting]])
        document.getElementById(id)?.setAttribute('aria-pressed',String(active));
    }
    document.getElementById('chart-navigate')?.addEventListener('click',()=>setMode('navigate'));
    document.getElementById('chart-select')?.addEventListener('click',()=>setMode('select'));
    function ordersOverlay(orders){
      const desired=new Map(orders.map(order=>[order.side+':'+order.price,order]));
      for(const [key,line] of lines)if(!desired.has(key)){candles.removePriceLine(line);lines.delete(key);}
      for(const [key,order] of desired)if(!lines.has(key))lines.set(key,candles.createPriceLine({
        price:Number(order.price),color:order.side==='bid'?'#85c6b580':'#ff874a80',lineWidth:1,lineStyle:2,axisLabelVisible:false,title:''}));
    }
    return {
      update(payload){
        const next=(payload.candle_seconds||86400)+':'+Boolean(payload.window)+':'+JSON.stringify(payload.candles);
        if(next===signature)return false;
        const samePeriod=replayCandleSeconds===(payload.candle_seconds||86400);
        const logical=chart.timeScale().getVisibleLogicalRange();
        // Time ranges stop at actual candles. Logical ranges also retain blank
        // space beyond the last candle, including a view entirely in that space.
        const anchor=logical&&axisTimes.length?Math.max(0,Math.min(axisTimes.length-1,Math.floor(logical.from))):null;
        const anchorTime=anchor===null?null:axisTimes[anchor];
        signature=next;replayCandleSeconds=payload.candle_seconds||86400;replayMemo=null;
        const visible=chart.timeScale().getVisibleRange();
        // Detailed windows supplement the overview. They must not delete the
        // surrounding history and clamp the user's scroll back to a new edge.
        const incoming=(payload.candles||[]).map(b=>({...b,seconds:replayCandleSeconds}));
        if(payload.window&&bars.length&&incoming.length){
          const first=incoming[0].time,last=incoming.at(-1).time+replayCandleSeconds;
          bars=[...bars.filter(b=>b.time+b.seconds<=first||b.time>=last),...incoming].sort((a,b)=>a.time-b.time);
        }else if(!payload.window||!bars.length)bars=incoming;
        points=bars.flatMap(b=>[{block:b.from_block,time:b.time},{block:b.to_block,time:b.time}]);
        const data=bars.map(b=>({time:b.time,open:b.open,high:b.high,low:b.low,close:b.close}));
        const times=new Set(data.map(b=>b.time));
        // Match whitespace to the selected period. Daily placeholders between
        // weekly candles made every body one seventh of its intended width.
        if(bars.length)for(let t=bars[0].time;t<bars.at(-1).time;t+=Math.max(86400,replayCandleSeconds))if(!times.has(t))data.push({time:t});
        data.sort((a,b)=>a.time-b.time);
        axisTimes=data.map(b=>b.time);
        candles.setData(data);
        chart.applyOptions({timeScale:{timeVisible:replayCandleSeconds<86400,tickMarkFormatter:time=>replayCandleSeconds>=86400?utc(time).slice(0,10):utc(time).slice(5,16)}});
        if(bars.length>1&&!fitted&&element.clientWidth>0){chart.timeScale().fitContent();fitted=true;}
        else if(samePeriod&&logical&&anchorTime!==null&&axisTimes.length){
          const newAnchor=lowerBound(axisTimes,anchorTime,time=>time);
          if(axisTimes[newAnchor]===anchorTime){
            // Earlier candles can arrive in a detailed response. Keep the
            // viewport anchored to the same candle, not its former array index.
            const shift=newAnchor-anchor;
            chart.timeScale().setVisibleLogicalRange({from:logical.from+shift,to:logical.to+shift});
          }else if(visible)chart.timeScale().setVisibleRange(visible);
        }else if(visible&&bars.length)chart.timeScale().setVisibleRange(visible);
        requestPaint();return true;
      },
      select(first,last){if(selected?.[0]===first&&selected?.[1]===last)return;selected=[first,last];requestPaint();},
      fit(){fitted=bars.length>0;chart.timeScale().fitContent();root.dispatchEvent(new CustomEvent('chart-window-changed',{detail:null}));},
      setMode,
      clear(){
        markers.setMarkers([]);
        if(equity){equity.setData([]);hold.setData([]);chart.removeSeries(equity);chart.removeSeries(hold);equity=null;hold=null;}
        if(legend)legend.hidden=true;
        for(const line of lines.values())candles.removePriceLine(line);lines.clear();replayMemo=null;requestPaint();
      },
      replay(replay,index,orders){
        if(!equity){
          equity=chart.addSeries(L.LineSeries,{color:'#ff874a',lineWidth:2,priceLineVisible:false,lastValueVisible:true,pointMarkersVisible:true},1);
          hold=chart.addSeries(L.LineSeries,{color:'#8b95a3',lineWidth:1,priceLineVisible:false,lastValueVisible:true,pointMarkersVisible:true},1);
          if(legend)legend.hidden=false;
          resizeReplayPane();
        }
        if(!replayMemo||replayMemo.replay!==replay||index<replayMemo.index)replayMemo={replay,index:-1,points:new Map(),baseline:new Map(),fills:[]};
        const memo=replayMemo;
        if(index!==memo.index){
          const oldFills=memo.fills.length;
          for(let i=memo.index+1;i<=index;i++){
            const frame=replay.frames[i],time=Math.floor(Date.parse(frame.timestamp)/1000),bucket=Math.floor(time/replayCandleSeconds)*replayCandleSeconds;
            if(frame.cumulative_pnl!=null)memo.points.set(bucket,{time:bucket,value:Number(frame.cumulative_pnl)});
            if(frame.hold_baseline_pnl!=null)memo.baseline.set(bucket,{time:bucket,value:Number(frame.hold_baseline_pnl)});
            for(const fill of frame.fills||[]){
              const fillTime=fill.timestamp?Math.floor(Date.parse(fill.timestamp)/1000):time;
              let p=lowerBound(bars,fillTime,b=>b.time);if(p===bars.length||bars[p].time>fillTime)p--;
              const bar=bars[p];
              if(bar&&fillTime<bar.time+bar.seconds)memo.fills.push({time:bar.time,position:fill.side==='bid'?'belowBar':'aboveBar',color:fill.side==='bid'?'#85c6b5':'#ff874a',shape:fill.side==='bid'?'arrowUp':'arrowDown',text:(fill.side==='bid'?'Buy':'Sell')+' '+Number(fill.size||fill.filled_size||0).toFixed(2)});
            }
          }
          equity.setData([...memo.points.values()].sort((a,b)=>a.time-b.time));hold.setData([...memo.baseline.values()].sort((a,b)=>a.time-b.time));
          if(memo.index===-1||memo.fills.length!==oldFills)markers.setMarkers(memo.fills.sort((a,b)=>a.time-b.time));
          memo.index=index;
        }
        ordersOverlay(orders);
      },
      get bars(){return bars;},
      get range(){return chart.timeScale().getVisibleRange();}
    };
  }
  root.KuruChart={mount};
})(window);
