const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require.resolve('../frontend/page_loading.js'), 'utf8');

function page(parts) {
  const nodes = {
    'page-content': {hidden:true, dataset:{loadParts:parts}},
    'page-loading': {hidden:false},
    'page-loading-message': {textContent:'Loading'},
    'page-loading-retry': {hidden:true, addEventListener(type, handler){this.click=handler;}},
  };
  const events=[];
  const window={dispatchEvent(event){events.push(event.type);}, location:{reload(){events.push('reload');}}};
  const document={getElementById(id){return nodes[id];}, body:{setAttribute(name, value){this[name]=value;}}};
  vm.runInNewContext(source,{window,document,Event:class {constructor(type){this.type=type;}}});
  return {window,nodes,document,events};
}

for (const order of [['inventory','progress'],['progress','inventory']]) {
  const {window,nodes,document,events}=page('inventory,progress');
  window.PageLoading.ready(order[0]);
  assert.equal(nodes['page-content'].hidden,true,'A partial initial response cannot reveal the page');
  window.PageLoading.ready(order[1]);
  assert.equal(nodes['page-content'].hidden,false);
  assert.equal(nodes['page-loading'].hidden,true);
  assert.equal(document.body['aria-busy'],'false');
  window.PageLoading.ready(order[1]);
  assert.deepEqual(events,['page-ready'],'Polling must not repeat the initial reveal');
}
{
  const {window,nodes,events}=page('inventory,progress');
  window.PageLoading.ready('progress');
  window.PageLoading.error('Coverage request failed');
  assert.equal(nodes['page-content'].hidden,true);
  assert.equal(nodes['page-loading-message'].textContent,'Coverage request failed');
  assert.equal(nodes['page-loading-retry'].hidden,false);
  nodes['page-loading-retry'].click();
  assert.deepEqual(events,['reload']);
}
{
  const {window,nodes}=page('backtest');
  assert.equal(nodes['page-content'].hidden,true);
  window.PageLoading.ready('backtest');
  assert.equal(nodes['page-content'].hidden,false,'Partial historical coverage can open after metadata loads');
}
console.log('Page loading tests passed');
