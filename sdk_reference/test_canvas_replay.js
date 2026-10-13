const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require.resolve('../frontend/app.js'),'utf8');
const calls=[];
function drawing(){return {beginPath(){},moveTo(){},lineTo(){},stroke(){},fillText(){},setTransform(){},setLineDash(){calls.push(['orderLine']);},
  arc(x,y){calls.push(['fill',this.fillStyle,x,y]);},fill(){},drawImage(...args){calls.push(['prefix',args[7]]);}};}
const screen=drawing(),window={devicePixelRatio:1};let layers=0;
const frames=[{timestamp:'2026-01-01T00:00:00Z',bid:'1',ask:'1.2',midpoint:'1.1',marked_value:'100',hold_baseline_pnl:'0',fills:[]},
  {timestamp:'2026-01-01T00:02:00Z',bid:'1.1',ask:'1.3',midpoint:'1.2',marked_value:'101',hold_baseline_pnl:'0.5',fills:[{side:'bid',price:'1.1',timestamp:'2026-01-01T00:01:00Z'}]}];
const sandbox={window,app:{replay:{frames,timeline_sha256:'hash'},frameIndex:0,chartCache:new Map()},
  document:{createElement(){layers++;return {getContext:drawing};}},$:id=>id,
  prepareCanvas:()=>({context:screen,width:800,height:240}),activeOrdersAt:()=>[{side:'bid',price:'1.05'}],Map,Number,Math};
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf('function chartScale('),source.indexOf('function stopPlayback()')),sandbox);
sandbox.drawPriceChart();sandbox.drawEquityChart();
assert.equal(layers,2,'Price and equity use separate cached Canvas layers');
assert.equal(calls.filter(c=>c[0]==='fill').length,0,'Future fills must not be visible');
const initialWidth=calls.filter(c=>c[0]==='prefix').at(-1)[1];
sandbox.app.frameIndex=1;sandbox.drawPriceChart();sandbox.drawEquityChart();
assert.equal(layers,2,'Playback must not rebuild a full layer');
assert.ok(calls.filter(c=>c[0]==='prefix').at(-1)[1]>initialWidth,'Forward playback reveals more data');
const fill=calls.find(([kind])=>kind==='fill');
assert.equal(fill[1],'#8894ff','Bid fills use buy markers');
assert.equal(fill[2],422,'A fill halfway through elapsed time belongs halfway across the plot');
assert.equal(calls.filter(c=>c[0]==='orderLine').length,0,'No detached dashed order lines remain');
sandbox.app.frameIndex=0;sandbox.drawPriceChart();
assert.equal(calls.filter(c=>c[0]==='prefix').at(-1)[1],initialWidth,'Backward seek hides later data');
const timeScale=sandbox.replayTimeScale([frames[0],{...frames[0],timestamp:'2026-01-01T00:00:30Z'},frames[1]],{x:i=>i*100,y:v=>v});
assert.equal(timeScale.x(1),50,'Irregular frames are spaced by elapsed time, not frame count');
console.log('Canvas replay tests passed');
