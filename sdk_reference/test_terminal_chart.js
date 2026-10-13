const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const handlers={},rootHandlers={},events=[],series=[],labels={},timers=[];
let options,fits=0,range={from:60,to:240},logical={from:0,to:2},markerData=[],markerWrites=0,removedLines=0,timeRangeWrites=0;
const scale={timeToCoordinate:t=>t,coordinateToTime:x=>x,getVisibleRange:()=>range,setVisibleRange:r=>{range=r;timeRangeWrites++;},
getVisibleLogicalRange:()=>logical,setVisibleLogicalRange:r=>logical=r,fitContent:()=>fits++,subscribeVisibleTimeRangeChange:()=>{},subscribeVisibleLogicalRangeChange:()=>{}};
const context={fillRect(){},strokeRect(){},setTransform(){},clearRect(){},save(){},restore(){},beginPath(){},rect(){},clip(){}};
const element={clientWidth:800,clientHeight:530,append(){},getBoundingClientRect:()=>({left:0,top:0}),setPointerCapture(){},addEventListener:(name,fn)=>handlers[name]=fn};
const chart={applyOptions(){},removeSeries(){},addSeries(){const item={data:[],writes:0,created:0,setData(value){this.data=value;this.writes++;logical={from:0,to:Math.max(0,value.length-1)};},createPriceLine(value){this.created++;return value;},removePriceLine(){removedLines++;}};series.push(item);return item;},panes:()=>[{getHeight:()=>400}, {getHeight:()=>90,setHeight(){}}],timeScale:()=>scale,subscribeCrosshairMove(){}};
const window={dispatchEvent:event=>events.push(event),addEventListener:(name,fn)=>rootHandlers[name]=fn};
const sandbox={window,LightweightCharts:{createChart(el,value){options=value;return chart;},createSeriesMarkers:()=>({setMarkers:value=>{markerData=value;markerWrites++;}})},document:{createElement:()=>({getContext:()=>context}),getElementById:id=>labels[id]},ResizeObserver:class{observe(){}},devicePixelRatio:1,Date,Map,Set,CustomEvent:class{constructor(type,init={}){this.type=type;this.detail=init.detail;}},requestAnimationFrame:fn=>1,setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){}};
vm.runInNewContext(fs.readFileSync(require.resolve('../frontend/terminal_chart.js'),'utf8'),sandbox);
const ui=window.KuruChart.mount(element);
const bars=[60,120,240].map((time,i)=>({time,from_block:10+i*10,to_block:19+i*10,open:1,high:2,low:1,close:2}));
assert.equal(ui.update({candles:bars,candle_seconds:60}),true);
assert.equal(fits,1);
assert.ok(options.localization.timeFormatter(60).includes('1970-01-01 00:01:00 UTC'));
assert.equal(options.timeScale.visible,true);
assert.equal(options.timeScale.fixRightEdge,false,'Panning beyond the last candle is allowed');
assert.equal(options.handleScroll.pressedMouseMove,true,'Navigation is available by default');
const previousWrites=series[0].writes;
assert.equal(ui.update({candles:bars,candle_seconds:60}),false);
assert.equal(series[0].writes,previousWrites,'A status-only poll does not redraw candles');
ui.update({candles:[...bars,{...bars[2],time:300,from_block:40,to_block:49}],candle_seconds:60});
assert.equal(fits,1,'A background update must not reset zoom');
assert.deepEqual(range,{from:60,to:240});

ui.select(10,39);ui.setMode('select');
const pointer=(x,y=100)=>({button:0,clientX:x,clientY:y,pointerId:1,target:{closest:()=>null}});
handlers.pointerdown(pointer(60));handlers.pointerup();
assert.equal(events.filter(e=>e.type==='chart-range-selected').length,0,'A click must not submit an old selection');
handlers.pointerdown(pointer(60));handlers.pointermove(pointer(240));handlers.pointerup();
const selection=events.find(e=>e.type==='chart-range-selected');
assert.equal(selection.detail.from_block,10);assert.equal(selection.detail.to_block,39);
handlers.pointerdown(pointer(700,500));handlers.pointerup();
assert.equal(events.filter(e=>e.type==='chart-range-selected').length,1,'The date axis cannot select a period');
ui.setMode('navigate');handlers.pointerdown(pointer(60));rootHandlers.pointerup();timers.at(-1)();
assert.equal(events.at(-1).type,'chart-window-changed','Navigation ending outside the chart resumes data requests');
handlers.wheel();timers.at(-1)();assert.equal(events.at(-1).type,'chart-window-changed');

ui.update({candles:[{...bars[1],time:120,close:1.5}],candle_seconds:60,window:true});
assert.deepEqual(Array.from(ui.bars,b=>b.time),[60,120,240,300],'A zoom response retains surrounding history');
assert.deepEqual(range,{from:60,to:240},'A response preserves the current viewport');

const replay={frames:[{timestamp:'1970-01-01T00:04:00Z',cumulative_pnl:2,hold_baseline_pnl:1,fills:[{timestamp:'1970-01-01T00:01:15Z',side:'bid',size:3}]},
{timestamp:'1970-01-01T00:05:00Z',cumulative_pnl:3,hold_baseline_pnl:1,fills:[]}]};
const orders=[{price:1,side:'bid'}];
ui.replay(replay,0,orders);
assert.equal(markerData[0].time,60,'Fill markers use their own timestamp');
assert.equal(markerData[0].shape,'arrowUp');assert.equal(series[1].data[0].value,2);
const writes=series[1].writes,created=series[0].created,markers=markerWrites;
ui.replay(replay,0,orders);
assert.equal(series[1].writes,writes,'An unchanged replay frame is not rebuilt');
assert.equal(series[0].created,created,'Unchanged order lines are retained');
ui.replay(replay,1,orders);
assert.equal(markerData.length,1,'Forward replay must not double-count earlier fills');
assert.equal(markerWrites,markers,'No-fill frames do not rebuild fill markers');
ui.replay(replay,0,[]);
assert.equal(series[1].data.length,1,'Backward seeks remove later P&L');assert.ok(removedLines>0);
ui.clear();assert.equal(markerData.length,0);assert.equal(series[1].data.length,0);
ui.fit();assert.equal(events.at(-1).detail,null);
const monday=345600;
ui.update({candles:[{...bars[0],time:monday},{...bars[1],time:monday+2*604800}],candle_seconds:604800});
assert.equal(series[0].data.length,3,'Weekly candles have one empty week, not daily spacer slots');
assert.equal(series[0].data[1].time,monday+604800);

// The time API clips the viewport to actual dates, but the logical viewport
// must keep the empty right-hand space through polls and detailed responses.
logical={from:.25,to:12.75};
const beforeTimeWrites=timeRangeWrites;
ui.update({candles:[{...bars[0],time:monday},{...bars[1],time:monday+2*604800,close:1.75}],candle_seconds:604800});
assert.equal(logical.from,.25);assert.equal(logical.to,12.75);
assert.equal(timeRangeWrites,beforeTimeWrites,'An update must not clamp a future viewport to candle dates');
logical={from:15.5,to:30.5};range=null;
ui.update({candles:[{...bars[0],time:monday},{...bars[1],time:monday+2*604800},{...bars[2],time:monday+3*604800}],candle_seconds:604800});
assert.equal(logical.from,15.5);assert.equal(logical.to,30.5,'Even a wholly blank viewport remains in place');
ui.update({candles:[{...bars[0],time:monday-604800}],candle_seconds:604800,window:true});
assert.equal(logical.from,16.5);assert.equal(logical.to,31.5,'Prepending data retains the same anchored view and zoom');
ui.update({candles:[{...bars[1],time:monday+2*604800,close:1.25}],candle_seconds:604800,window:true});
assert.equal(logical.from,16.5);assert.equal(logical.to,31.5,'A detailed response must preserve right-hand whitespace');
assert.equal(fits,2,'Only initial layout and an explicit fit may reset the view');
console.log('Terminal chart tests passed');
