/* UI adapter: every displayed record and metric comes from the local pipeline API. */
'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const present = value => value !== null && value !== undefined && value !== '';
const number = value => present(value) && Number.isFinite(Number(value)) ? Number(value).toLocaleString() : 'Not available';
const money = (value, type = '') => present(value) ? (type === 'percentage' ? `${number(value)}% of charges` : new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'}).format(Number(value))) : 'Not available';
const percent = value => present(value) ? `${Number(value).toFixed(2)}%` : 'Not available';
const state = {active:'overview', pages:{rates:1, plans:1, entities:1, benchmarks:1}, totals:{}, selected:new Map(), rates:new Map(), running:false, runId:null, offset:0, poll:null, versions:{}, exports:{}};

async function api(path, options = {}) {
  const response = await fetch(path, {cache:'no-store', ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
function message(text = '') {
  $('message').textContent = text;
  $('message').hidden = !text;
}
function fail(error) { message(error.message || String(error)); }
function formQuery(id) { return new URLSearchParams(new FormData($(id))); }
function empty(text = 'No results available yet.') { return `<p class="muted empty-state">${esc(text)}</p>`; }
function table(columns, rows) {
  if (!rows.length) return empty();
  return `<div class="table-responsive"><table><thead><tr>${columns.map(c=>`<th>${esc(c[0])}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${columns.map(c=>`<td>${esc(typeof c[1] === 'function' ? c[1](r) : r[c[1]])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function list(values, render = number) {
  const entries = Object.entries(values || {});
  return entries.length ? `<dl class="metric-list">${entries.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(render(v))}</dd>`).join('')}</dl>` : empty();
}
function updateExports() {
  document.querySelectorAll('[data-export]').forEach(button=>{ button.disabled = state.running || !state.exports[button.dataset.export]; });
}
async function loadOverview() {
  const summary = await api('/api/summary');
  const m = summary.metrics;
  const cards = [
    ['Index plan / file rows', number(m.totalIndexPlans)], ['California group plans', number(m.caGroupPlans)],
    ['PPO plans', number(m.ppoPlans)], ['NPIs with a verified address', percent(m.addressVerificationPct)],
    ['Confirmed PPO rate rows', number(m.confirmedRows)], ['Candidate rate rows', number(m.candidateRows)],
  ];
  $('kpis').innerHTML = cards.map(([label,value])=>`<article class="neu-card kpi-card"><p class="kpi-title">${esc(label)}</p><div class="kpi-value-row"><span class="kpi-number">${esc(value)}</span></div></article>`).join('');
  const scores = Object.entries(m.fileScores || {});
  $('overviewQuality').innerHTML = scores.length ? table([['File',r=>r.file.split('/').pop()],['Score','score'],['Grade','grade'],['Status','status']], scores.map(([file,r])=>({file,...r}))) : empty('C3 has not produced quality scores.');
  $('planDistribution').innerHTML = list(m.planTypes);
  $('costTiers').innerHTML = list(m.costTiers);
  $('stageReports').innerHTML = table([['Stage','stage'],['Last completed','finished']], Object.entries(summary.finished).map(([stage,finished])=>({stage:stage.toUpperCase(),finished:finished || 'Not run'})));
  $('dataState').textContent = !summary.available ? 'No pipeline results yet. Open Run pipeline to configure inputs and start processing.' : 'Showing available pipeline outputs. Stage dates below identify the last completed reports.';
}
async function loadFacets() {
  const values = await api('/api/rates/facets');
  for (const [id,key,label] of [['filterCode','codes','All codes'],['filterSpecialty','specialties','All specialties'],['filterStatus','statuses','All statuses'],['filterTier','tiers','All tiers']]) {
    const select = $(id), previous = select.value;
    select.innerHTML = `<option value="">${label}</option>` + values[key].map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
    select.value = values[key].includes(previous) ? previous : '';
  }
}
function pager(name, data) {
  state.pages[name] = data.page;
  state.totals[name] = data.total;
  const pages = Math.max(1,Math.ceil(data.total/data.pageSize));
  $(name === 'rates' ? 'ratesPage' : `${name}Page`).textContent = `Page ${data.page} of ${pages} · ${number(data.total)} rows`;
  if (name === 'rates') {
    $('ratesPrev').disabled = data.page <= 1;
    $('ratesNext').disabled = data.page >= pages;
  } else {
    document.querySelector(`[data-page="${name}:-1"]`).disabled = data.page <= 1;
    document.querySelector(`[data-page="${name}:1"]`).disabled = data.page >= pages;
  }
}
function variant(r) { return [r.code_type,r.code,r.negotiated_type,r.billing_class,r.setting,r.pos,r.modifier].map(v=>v ?? '').join(' · '); }
async function loadRates() {
  const version = state.versions.rates = (state.versions.rates || 0) + 1;
  const query = formQuery('rateFilters');
  query.set('page',state.pages.rates); query.set('pageSize','25');
  const data = await api(`/api/rates?${query}`);
  if (version !== state.versions.rates) return;
  state.rates = new Map(data.rates.map(r=>[r.id,r]));
  $('rateCount').textContent = `${number(data.total)} matching practice-site rate rows`;
  $('ratesBody').innerHTML = data.rates.length ? data.rates.map(r=>`<tr>
    <td><input type="checkbox" data-compare="${esc(r.id)}" aria-label="Compare ${esc(r.name || r.npi)}" ${state.selected.has(r.id)?'checked':''}></td>
    <td><strong>${esc(r.name || 'Name unavailable')}</strong><br>NPI ${esc(r.npi)}<br>TIN ${esc(r.tin)}</td>
    <td>${esc(r.address || 'Address unavailable')}<br><small>${esc(r.verified === null ? 'Verification unavailable' : r.verified ? 'Verified by C5' : 'Not independently verified')}</small></td>
    <td><strong>${esc(r.code_type)} ${esc(r.code)}</strong><br><small>${esc([r.negotiated_type,r.billing_class,r.setting,r.pos,r.modifier].filter(Boolean).join(' · '))}</small></td>
    <td>${esc(r.specialty || 'Specialty unavailable')}</td>
    <td>${esc(money(r.rate,r.negotiated_type))}</td>
    <td>${present(r.cost_index)?`${esc(Number(r.cost_index).toFixed(3))}×`:'Not scored'}<br>${esc(r.tier || '')}<br><small>${esc(r.score_confidence ? `Confidence: ${r.score_confidence}` : '')}</small></td>
    <td>${esc(r.network_status)}</td><td><button class="neu-btn neu-btn-sm" data-inspect="${esc(r.id)}">Inspect</button></td>
  </tr>`).join('') : '<tr><td colspan="9">No rates match these filters.</td></tr>';
  pager('rates',data);
}
function renderComparison() {
  const rows = [...state.selected.values()];
  if (!rows.length) { $('comparison').innerHTML = empty('Select up to four rows from the rate explorer.'); return; }
  const comparable = rows.every(r=>variant(r)===variant(rows[0])) && rows.every(r=>r.network_status===rows[0].network_status);
  $('comparison').innerHTML = `<p class="notice">${comparable?'These selections share the billing code, price variant and network status.':'Selections have different codes, price variants or network statuses. Compare each within its matching C7 cohort.'}</p>` + table([
    ['Provider',r=>r.name || r.npi],['NPI','npi'],['TIN','tin'],['Code and variant',variant],['Rate',r=>money(r.rate,r.negotiated_type)],['Network','network_status'],['C7 tier',r=>r.tier || 'Not scored'],
  ],rows);
}
function inspect(id) {
  const r = state.rates.get(id);
  if (!r) return;
  const values = {'Provider':r.name || 'Unavailable','NPI':r.npi,'TIN':r.tin,'Practice address':r.address || 'Unavailable',
    'Specialty':r.specialty,'Specialty source':r.specialty_source,'Code / price variant':variant(r),'Code description':r.description,
    'Negotiated rate':money(r.rate,r.negotiated_type),'Network status':r.network_status,'Network evidence':r.network_basis || 'Unavailable',
    'Address verified':r.verified === null ? 'Unavailable' : r.verified?'Yes':'No','Address confidence':r.address_confidence,
    'Address sources':r.address_sources,'Address evidence':r.address_evidence,'C7 cost index':r.cost_index,
    'Cost tier':r.tier || 'Not scored','Score confidence':r.score_confidence || 'Unavailable','NPI deactivated':r.deactivated === null?'Unavailable':r.deactivated?'Yes':'No'};
  $('providerDetails').innerHTML = list(values, value=>present(value)?value:'Unavailable');
  $('providerDialog').showModal();
}
async function loadPlans() {
  const query = formQuery('planFilters'); query.set('page',state.pages.plans);
  const data = await api(`/api/c2/plans?${query}`);
  $('plansTable').innerHTML = data.available ? table([['Plan','plan_name'],['Plan ID','plan_id'],['Market','market_type'],['Scope','in_scope_ppo'],['Network','final_network_type'],['Confidence','confidence'],['Decision rule','decided_by'],['Reason','reason']], data.rows) : empty('Run C2 to generate plan classifications.');
  pager('plans',data);
}
async function loadEntities() {
  const query = formQuery('entityFilters'); query.set('page',state.pages.entities);
  const data = await api(`/api/c5/entities?${query}`);
  if (data.tins) {
    $('tinOptions').innerHTML = data.tins.map(t=>`<option value="${esc(t)}"></option>`).join('');
    $('entitiesTable').innerHTML = empty('Choose a TIN to inspect its practice sites.');
    return;
  }
  $('entitiesTable').innerHTML = table([['NPI','npi'],['Provider','name'],['Site rank','rank'],['Practice address','address'],['Verified',r=>r.verified?'Yes':'No'],['Confidence','confidence'],['Sources','sources'],['Evidence','evidence']],data.rows);
  pager('entities',data);
}
async function loadBenchmarks() {
  const query = formQuery('benchmarkFilters'); query.set('page',state.pages.benchmarks);
  const data = await api(`/api/benchmarks?${query}`);
  $('benchmarksTable').innerHTML = data.available ? table([
    ['NPI','npi'],['TIN','tin'],['Specialty','specialty'],['Region','region'],['Code / variant',variant],
    ['Rate',r=>money(r.rate,r.negotiated_type)],['Peer Q1',r=>money(r.peer_q1)],['Peer median',r=>money(r.peer_median)],['Peer Q3',r=>money(r.peer_q3)],['Peers','peer_n'],['Rate / median','rate_index'],['Peer level','peer_level'],
  ],data.rows) : empty('Run C7 for the selected cohort to generate peer benchmarks.');
  pager('benchmarks',data);
}
async function loadQuality() {
  const data = await api('/api/c3/metrics');
  $('qualityScores').innerHTML = table([['File',r=>r.rate_file_url?.split('/').pop()],['Bucket','bucket'],['Score','score'],['Grade','grade'],['Status','status'],['Sampled','sampled']],data.scores);
  $('qualityMetrics').innerHTML = table([['File',r=>r.rate_file_url?.split('/').pop()],['Bucket','bucket'],['Metric','metric'],['Measured value','value'],['Score (0–1)','score'],['Weight','weight'],['Measured','measured'],['Justification','justification']],data.metrics);
}
const loaders = {overview:loadOverview,rates:loadRates,plans:loadPlans,entities:loadEntities,benchmarks:loadBenchmarks,quality:loadQuality,pipeline:async()=>{}};
async function showTab(name) {
  state.active = name;
  document.querySelectorAll('.workspace-panel').forEach(panel=>{panel.hidden=panel.id!==name;});
  document.querySelectorAll('[data-tab]').forEach(button=>{
    button.classList.toggle('active',button.dataset.tab===name);
    if (button.dataset.tab===name) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
  });
  if (!state.running) await loaders[name]();
}
function updateRun(run) {
  if (run.id !== state.runId) {
    state.runId=run.id; state.offset=0;
    $('runLogs').textContent=run.id ? `Command: ${run.command.join(' ')}\n\n` : 'No run started in this server session.';
  }
  if (run.logsTruncated) $('runLogs').textContent += '\n[Earlier log lines are available in the full log file.]\n';
  for (const line of run.logs || []) {
    if (line.seq > state.offset) $('runLogs').textContent += line.text+'\n';
  }
  state.offset=run.nextOffset || 0;
  if ($('runLogs').textContent.length>300000) $('runLogs').textContent=$('runLogs').textContent.slice(-250000);
  $('runLogs').scrollTop=$('runLogs').scrollHeight;
  state.running=run.status==='running';
  $('runButton').disabled=state.running;
  $('runStatus').textContent = run.status==='idle'?'Idle':run.status==='running'?'Running…':`${run.status==='succeeded'?'Completed':'Failed'} · exit code ${run.exitCode}`;
  if (run.finished) $('runStatus').textContent += ` · ${new Date(run.finished).toLocaleString()}`;
  $('backendStatusText').textContent=state.running?'Pipeline running':run.status==='failed'?'Last run failed':$('backendStatusText').textContent;
  updateExports();
}
async function pollRun() {
  clearTimeout(state.poll);
  try {
    const run=await api(`/api/pipeline/run?after=${state.offset}`);
    updateRun(run);
    if (state.running) state.poll=setTimeout(pollRun,1000);
    else {
      if(run.status==='failed')message(`Pipeline failed with exit code ${run.exitCode}. Open Run pipeline to inspect the actual error output.`);
      await refresh(false);
    }
  } catch(error) { fail(error); state.poll=setTimeout(pollRun,2500); }
}
async function refresh(clearMessage=true) {
  if(clearMessage)message();
  const status=await api(`/api/status?after=${state.offset}`);
  state.exports=status.exports || {};
  $('pipelinePath').textContent=`Pipeline folder: ${status.dataDirectory.replace(/[\\/]data$/,'')} · Output folder: ${status.outputDirectory}`;
  $('backendStatusText').textContent=status.ratesReady?'Pipeline results loaded':'No C6 rate results yet';
  updateRun(status.run);
  if(state.running){clearTimeout(state.poll);state.poll=setTimeout(pollRun,1000);return;}
  state.selected.clear();renderComparison();
  await loadOverview();
  if(status.ratesReady)await loadFacets();
  if(state.active!=='overview')await loaders[state.active]();
}
async function configureRun() {
  const config=await api('/api/pipeline/config');
  for(const id of ['fromStage','toStage'])$(id).innerHTML=config.stages.map(s=>`<option value="${esc(s)}">${esc(s.toUpperCase())}</option>`).join('');
  $('toStage').value='c7';$('workbook').value=config.workbook;$('ppoCheck').value=config.ppo_check_csv;$('asOf').value=config.as_of;
}
async function startRun(event) {
  event.preventDefault();message();
  const form=new FormData($('runForm')), options={};
  for(const [key,value] of form.entries()){
    if(['fetch_public','check_directory'].includes(key))options[key]=true;
    else if(key==='workers')options[key]=Number(value);
    else if(key==='rate_files')options[key]=String(value).split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
    else if(String(value).trim())options[key]=String(value).trim();
  }
  $('runButton').disabled=true;
  try {
    const run=await api('/api/pipeline/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(options)});
    updateRun(run);await showTab('pipeline');state.poll=setTimeout(pollRun,500);
  }catch(error){$('runButton').disabled=state.running;throw error;}
}
function bindForm(id,name,loader) { $(id).addEventListener('submit',event=>{event.preventDefault();state.pages[name]=1;loader().catch(fail);}); }
async function initialize() {
  document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{message();showTab(button.dataset.tab).catch(fail);}));
  $('refreshButton').addEventListener('click',()=>refresh().catch(fail));
  bindForm('rateFilters','rates',loadRates);bindForm('planFilters','plans',loadPlans);bindForm('entityFilters','entities',loadEntities);bindForm('benchmarkFilters','benchmarks',loadBenchmarks);
  $('ratesPrev').addEventListener('click',()=>{state.pages.rates=Math.max(1,state.pages.rates-1);loadRates().catch(fail);});
  $('ratesNext').addEventListener('click',()=>{state.pages.rates++;loadRates().catch(fail);});
  document.querySelectorAll('[data-page]').forEach(button=>button.addEventListener('click',()=>{const [name,step]=button.dataset.page.split(':');state.pages[name]=Math.max(1,state.pages[name]+Number(step));loaders[name]().catch(fail);}));
  document.querySelectorAll('[data-export]').forEach(button=>button.addEventListener('click',async()=>{
    try {
      const response=await fetch(`/api/export?kind=${encodeURIComponent(button.dataset.export)}`);
      if(!response.ok)throw new Error((await response.json()).error);
      const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');
      a.href=url;a.download=response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'pipeline-export';
      a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(error){fail(error);}
  }));
  $('ratesBody').addEventListener('click',event=>{const button=event.target.closest('[data-inspect]');if(button)inspect(button.dataset.inspect);});
  $('ratesBody').addEventListener('change',event=>{
    const input=event.target;if(!input.matches('[data-compare]'))return;
    const id=input.dataset.compare;
    if(input.checked){if(state.selected.size>=4){input.checked=false;message('Select up to four rate rows.');return;}state.selected.set(id,state.rates.get(id));}
    else state.selected.delete(id);
    renderComparison();
  });
  $('clearCompare').addEventListener('click',()=>{state.selected.clear();renderComparison();document.querySelectorAll('[data-compare]').forEach(el=>{el.checked=false;});});
  $('closeDialog').addEventListener('click',()=>$('providerDialog').close());
  $('runForm').addEventListener('submit',event=>startRun(event).catch(fail));
  await configureRun();await refresh();
}
document.addEventListener('DOMContentLoaded',()=>initialize().catch(fail));
