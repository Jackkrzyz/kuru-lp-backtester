/* Local coverage and explicit download actions. Never start work on page load. */
(() => {
  const $ = id => document.getElementById(id);
  const number = value => Number(value || 0).toLocaleString();
  const bytes = value => {
    const n = Number(value || 0), unit = n >= 2**30 ? 'GiB' : n >= 2**20 ? 'MiB' : 'KiB';
    return (n / (unit === 'GiB' ? 2**30 : unit === 'MiB' ? 2**20 : 2**10)).toFixed(2) + ' ' + unit;
  };
  let state = null, busy = false, initialized = false, rendered = null;
  function error(message) { $('suite-error').textContent = message || ''; $('suite-error').hidden = !message; }
  function controls() {
    const running = state?.jobs?.some(job => job.running);
    $('start-download').disabled = busy || running || !state?.controls_available || !state?.token_available || !state?.scope_is_finalized || !$('confirm-download').checked;
    $('stop-download').disabled = busy || !running;
    $('refresh-head').disabled = busy || !state?.controls_available;
  }
  function render(value) {
    state = value;
    const signature=JSON.stringify(value);
    if(signature===rendered){controls();return;}
    rendered=signature;
    const first = value.deployment_block, last = value.scope_to_block, total = last-first+1;
    $('deployment-block').textContent = number(first);
    $('latest-block').textContent = number(last);
    $('network-scope').textContent = value.scope_is_finalized
      ? `${number(total)} blocks since deployment · finalized at ${number(last)} · checked ${new Date(value.network.checked_at*1000).toISOString().replace('T',' ').slice(0,19)} UTC. Network event count is not known.`
      : `Latest network block not checked. Chart ends at the latest locally known block: ${number(last)}. Check the finalized block to show the full target.`;
    const target = $('archive-coverage'); target.replaceChildren();
    for (const [name,key,style] of [['Downloaded L3','downloaded_ranges','downloaded'],['Verified L3','verified_ranges','verified'],['Trade discovery','trade_only_ranges','trade']]) {
      const row=document.createElement('div'); row.className=`archive-lane ${style}`;
      const label=document.createElement('span'); label.textContent=name;
      const track=document.createElement('div'); track.className='archive-track';
      track.setAttribute('role','img'); track.setAttribute('aria-label',`${name} block coverage`);
      let covered=0;
      for (const range of value[key] || []) {
        const start=Math.max(first,range.from_block), end=Math.min(last,range.to_block);
        if (start>end) continue;
        covered+=end-start+1;
        const bar=document.createElement('div'); bar.className='archive-segment';
        bar.style.left=((start-first)/total*100)+'%'; bar.style.width=((end-start+1)/total*100)+'%';
        bar.title=`${number(start)}–${number(end)}`; track.append(bar);
      }
      const percent=document.createElement('span'); percent.textContent=value.inventory_ready===false?'—':(covered/total*100).toFixed(3)+'%';
      row.append(label,track,percent);target.append(row);
    }
    const ready=value.inventory_ready!==false;
    $('saved-blocks').textContent=ready?number(value.downloaded_blocks):'—';
    $('verified-blocks').textContent=ready?number(value.verified_blocks):'—';
    $('saved-size').textContent=ready?bytes(value.total_saved_bytes):'—';
    $('free-size').textContent=ready?bytes(value.free_disk_bytes):'—';
    $('inventory-status').textContent=value.inventory_error?`Storage scan failed: ${value.inventory_error}. Saved totals are retained.`
      : !ready?'Checking saved files in the background. Progress remains available.'
      : `${value.inventory_updated_at?'Storage checked '+new Date(value.inventory_updated_at*1000).toISOString().slice(11,19)+' UTC. ':''}${value.inventory_refreshing?'Refreshing in the background.':'Storage totals refresh at most once per minute.'}`;
    const detail=$('storage-details');detail.replaceChildren();
    for(const [label,amount] of Object.entries(value.storage_bytes || {})) {
      const row=document.createElement('div');row.className='detail';
      const name=document.createElement('span'), size=document.createElement('span');
      name.textContent=label;size.textContent=bytes(amount);row.append(name,size);detail.append(row);
    }
    const note=document.createElement('p');note.className='suite-note';
    note.textContent=ready?`${number(value.saved_file_count)} files. Includes local caches and result files, not the SDK or source code. ${value.coverage_basis || ''} ${(value.warnings || []).join(' ')}`:'File totals and coverage are not yet measured.';detail.append(note);
    for (const [label,key] of [['Downloaded L3','downloaded_ranges'],['Verified L3','verified_ranges'],['Trade discovery','trade_only_ranges']]) {
      const ranges=document.createElement('p');ranges.className='suite-note';
      ranges.textContent=label+': '+(!ready?'Not yet measured':(value[key] || []).map(range=>`${number(range.from_block)}–${number(range.to_block)}`).join(', ') || 'None');detail.append(ranges);
    }
    if(!initialized&&ready) {
      const end=Math.max(first,...(value.downloaded_ranges || []).map(range=>range.to_block))+1;
      $('download-from').value=end; $('download-to').value=end+4999;initialized=true;
    }
    $('download-message').textContent=!value.controls_available ? 'Read-only monitor. Open the Data suite from the backtest server to start downloads.'
      : !value.token_available ? 'Set ENVIO_API_TOKEN in the server terminal or local .env. The token stays on the server.'
      : 'Start resumes the same range from its saved cursor. Stop affects only the job started by this server. Validation and promotion remain separate commands.';
    const jobs=$('download-jobs');jobs.replaceChildren();
    for(const job of value.jobs || []) {
      const row=document.createElement('div');row.className='download-job';
      const text=document.createElement('span');
      text.textContent=`${job.job_id} · ${number(job.request.from_block)}–${number(job.request.to_block)} · ${job.running ? 'running' : job.phase} · ${number(job.progress?.downloaded_events)} events${job.progress?.error ? ' · '+job.progress.error : ''}`;
      const button=document.createElement('button');button.type='button';button.className='secondary-button';button.textContent='Select range';
      button.disabled=job.running;button.addEventListener('click',()=>{$('download-from').value=job.request.from_block;$('download-to').value=job.request.to_block;$('confirm-download').checked=false;controls();});
      row.append(text,button);jobs.append(row);
    }
    controls();
  }
  async function request(path, payload) {
    const response=await fetch(path,{method:payload===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},body:payload===undefined?undefined:JSON.stringify(payload),cache:'no-store',signal:AbortSignal.timeout(30000)});
    const result=await response.json();if(!response.ok)throw new Error(result.error || `HTTP ${response.status}`);return result;
  }
  async function poll() {
    try { render(await request('/api/data-suite'));error(''); } catch(e) { error(e.message); }
    setTimeout(poll,document.hidden?30000:5000);
  }
  async function action(path,payload) {
    busy=true;controls();error('');
    try { await request(path,payload);render(await request('/api/data-suite')); }
    catch(e) { error(e.message); }
    finally { busy=false;controls(); }
  }
  $('refresh-head').addEventListener('click',()=>action('/api/data-suite/head',{}));
  $('stop-download').addEventListener('click',()=>action('/api/data-suite/stop',{}));
  $('confirm-download').addEventListener('change',controls);
  for(const id of ['download-from','download-to']) $(id).addEventListener('input',()=>{initialized=true;$('confirm-download').checked=false;controls();});
  $('download-form').addEventListener('submit',event=>{
    event.preventDefault();if($('start-download').disabled)return;
    action('/api/data-suite/download',{market:state.market,from_block:Number($('download-from').value),to_block:Number($('download-to').value),confirm_download:$('confirm-download').checked});
  });
  poll();
})();
