/* Display diagnostics only. Never changes engine accounting or saved hashes. */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.ReplayAnalytics=api;})(typeof globalThis!=='undefined'?globalThis:this,()=>{
  function sharpe(frames,periodSeconds=60,minReturns=30){
    const metadata={period_seconds:periodSeconds,risk_free_rate:0,annualized:false,estimated:true,sample_count:0,value:null};
    if(!Number.isFinite(periodSeconds)||periodSeconds<=0)return {...metadata,reason:'Invalid sample period'};
    const points=frames.map((f,i)=>({time:Date.parse(f.timestamp)/1000,value:Number(f.marked_value),index:i}));
    if(points.length<2||points.some(p=>!Number.isFinite(p.time)||!Number.isFinite(p.value)||p.value<=0))
      return {...metadata,reason:'Positive marked values and timestamps are required'};
    points.sort((a,b)=>a.time-b.time||a.index-b.index);
    const first=Math.ceil(points[0].time/periodSeconds)*periodSeconds,last=Math.floor(points.at(-1).time/periodSeconds)*periodSeconds;
    if((last-first)/periodSeconds>1000000)return {...metadata,reason:'Too many minute samples'};
    let cursor=0,previous=null,n=0,mean=0,m2=0;
    for(let stamp=first;stamp<=last;stamp+=periodSeconds){
      // Previous observation only: no interpolation from a future frame.
      while(cursor+1<points.length&&points[cursor+1].time<=stamp)cursor++;
      const value=points[cursor].value;
      if(previous!=null){const r=value/previous-1;n++;const delta=r-mean;mean+=delta/n;m2+=delta*(r-mean);}
      previous=value;
    }
    metadata.sample_count=n;
    if(n<minReturns)return {...metadata,reason:`Needs at least ${minReturns} complete ${periodSeconds/60}-minute returns`};
    const deviation=Math.sqrt(m2/(n-1));
    if(!Number.isFinite(deviation)||deviation<=Number.EPSILON)return {...metadata,reason:'Return volatility is zero'};
    return {...metadata,value:mean/deviation,reason:'Compact replay estimate; zero risk-free rate; not annualized'};
  }
  return {sharpe};
});
