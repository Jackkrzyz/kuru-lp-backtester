const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {constructor(){this._text='';this.style={};this.value='all';this.children=[];this.handlers={};this.attributes={};this.classList={toggle:(key,value)=>{this[key]=value;}};}get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}set textContent(v){this._text=v;this.children=[];}append(...v){this.children.push(...v);}replaceChildren(){this._text='';this.children=[];}addEventListener(n,f){this.handlers[n]=f;}setAttribute(k,v){this.attributes[k]=v;}focus(){this.focused=true;}}
const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
const saved=new Map(),link=new Element();
const context={window:{localStorage:{getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)}},document:{body:new Element(),getElementById:get,createElement:()=>new Element(),querySelectorAll:()=>[link]},Date,Map,Number,JSON};
vm.runInNewContext(fs.readFileSync(require.resolve('../frontend/terminal_jobs.js'),'utf8'),context);
const jobs=context.window.LabJobs;
get('jobs-dialog').showModal=function(){this.open=true;};get('jobs-dialog').close=function(){this.open=false;};
get('jobs-open').handlers.click();assert.equal(get('jobs-dialog').open,true);
get('jobs-close').handlers.click();assert.equal(get('jobs-dialog').open,false);
jobs.update({jobs:[{job_id:'download',phase:'downloading',running:true,request:{from_block:10,to_block:20},progress:{updated_at:100,downloaded_events:4,actions:[{kind:'request',sequence:1,timestamp:100,from_block:10,to_block_exclusive:21},{kind:'response',sequence:2,timestamp:101,log_count:4,block_count:2,next_block:21},{kind:'page_committed',sequence:3,timestamp:102,new_events:4,page_events:4,next_block:21}]}}],verifications:[]});
assert.equal(get('job-headline').textContent,'1 active job');
jobs.update({jobs:[{job_id:'download',phase:'downloading',running:true,request:{from_block:10,to_block:20},progress:{downloaded_block:15}}]});
assert.equal(get('job-detail').children[0].children[1].attributes['aria-valuenow'],'54.5');
assert.ok(get('job-detail').children[0].children[0].children.some(node=>node.className==='status-dots'));
assert.ok(get('activity-log').children.some(row=>row.children[2].textContent.includes('durable cursor 21')));
const count=get('activity-log').children.length;
jobs.update({jobs:[{job_id:'download',phase:'downloading',running:true,progress:{updated_at:100,actions:[{kind:'request',sequence:1,timestamp:100,from_block:10,to_block_exclusive:21}]}}]});
assert.equal(get('activity-log').children.length,count,'Polling must not duplicate actions');
jobs.update({jobs:[],verifications:[{job_id:'verify',phase:'failed',running:false,request:{from_block:10,to_block:20},progress:{updated_at:103,error:'RPC parity failed'}}]});
assert.ok(get('job-summary').children.some(row=>row.textContent.includes('RPC parity failed')));
get('log-filter').value='verification';get('log-filter').handlers.change();
assert.ok(get('activity-log').children.every(row=>row.children[1].textContent==='verification'));
jobs.backtest('run',{status:'completed',stage:'complete',processed_events:20});
jobs.chart({phase:'indexing_local_trades',updated_at:104});
assert.ok(get('job-summary').children.some(row=>row.textContent.includes('local-candles')));
jobs.update({jobs:[{job_id:'empty',phase:'downloading',progress:{validated_block:99,validated_events:0}}]});
assert.ok(!get('job-summary').children.find(row=>row.textContent.includes('empty')).textContent.includes('verified through'));
assert.equal(get('log-body').hidden,false);
get('log-toggle').handlers.click();
assert.equal(get('log-body').hidden,true);
assert.equal(get('log-toggle').attributes['aria-expanded'],'false');
assert.equal(saved.get('kurulab.logs.minimized'),'true');
jobs.chart({phase:'complete',processed_files:12});
assert.equal(get('log-body').hidden,true,'Polling must not reopen the log');
link.handlers.click({preventDefault(){}});
assert.equal(get('log-body').hidden,false);
assert.equal(get('log-toggle').textContent,'Minimize');
assert.equal(get('log-toggle').focused,true);
get('log-toggle').handlers.click();
jobs.update({jobs:[],verifications:[{job_id:'child',phase:'rpc_timestamps',running:true,progress:{timestamp_blocks_checked:50,timestamp_blocks_total:100}}],
verification_queue:{job_id:'queue',phase:'verifying',running:true,progress:{current_job_id:'child',completed_partitions:3,total_partitions:10},
current_progress:{current_partition:[10,20],timestamp_blocks_checked:50,timestamp_blocks_total:100,checkpoint_reused:true}}});
assert.equal(get('job-headline').textContent,'1 active job','A queue and its child are one running verification job');
assert.ok(get('job-detail').textContent.includes('timestamp checks 50/100'));
assert.ok(get('job-detail').textContent.includes('checkpoint reused'));
jobs.update({verifications:[{job_id:'rpc',phase:'rpc_timestamps',running:true,progress:{actions:[
{kind:'rpc_response',sequence:1,timestamp:200,methods:{eth_getBlockByNumber:50},seconds:.4,phase:'rpc_timestamps',partition:[10,20]},
{kind:'rpc_retry',sequence:2,timestamp:201,attempt:1,http_status:429,seconds:15,partition:[10,20]}]}}]});
assert.ok(get('activity-log').textContent.includes('eth_getBlockByNumber × 50'));
assert.ok(get('activity-log').textContent.includes('HTTP 429 · wait 15s'));
vm.runInNewContext(fs.readFileSync(require.resolve('../frontend/terminal_jobs.js'),'utf8'),context);
assert.equal(get('log-body').hidden,true,'The minimized state survives a page reload');
console.log('Indexing UI tests passed');
