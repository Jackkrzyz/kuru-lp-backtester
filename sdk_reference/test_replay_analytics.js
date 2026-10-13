const assert=require('node:assert/strict');
const {sharpe}=require('../frontend/replay_analytics');
const frames=Array.from({length:61},(_,i)=>({timestamp:new Date(Date.UTC(2026,0,1,0,i)).toISOString(),marked_value:100*Math.exp(.001*i+.005*(i%2))}));
const metric=sharpe(frames);
const returns=frames.slice(1).map((f,i)=>f.marked_value/frames[i].marked_value-1);
const mean=returns.reduce((a,b)=>a+b,0)/returns.length;
const sd=Math.sqrt(returns.reduce((a,b)=>a+(b-mean)**2,0)/(returns.length-1));
assert.ok(Math.abs(metric.value-mean/sd)<1e-12);
assert.equal(metric.sample_count,60);assert.equal(metric.annualized,false);assert.equal(metric.estimated,true);
assert.equal(sharpe(frames.slice(0,10)).value,null,'Short runs must not report a confident ratio');
assert.equal(sharpe(frames.map(f=>({...f,marked_value:100}))).value,null,'Zero volatility is undefined');
assert.equal(sharpe([{...frames[0],marked_value:0},frames[1]]).value,null);
const sparse=[{timestamp:'2026-01-01T00:00:00Z',marked_value:100},{timestamp:'2026-01-01T00:01:30Z',marked_value:200},
  {timestamp:'2026-01-01T00:03:00Z',marked_value:200}];
assert.ok(Math.abs(sharpe(sparse,60,2).value-1/Math.sqrt(3))<1e-12,'Samples use preceding state, never future interpolation');
assert.deepEqual(sharpe(frames),sharpe(frames),'Metrics are deterministic');
console.log('Replay analytics tests passed');
