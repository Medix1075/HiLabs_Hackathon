/* UI adapter: every displayed record and metric comes from the local pipeline API. */
'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const present = value => value !== null && value !== undefined && value !== '';
const number = value => present(value) && Number.isFinite(Number(value)) ? Number(value).toLocaleString() : 'Not available';
const usd = new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'});
const money = (value, type = '') => present(value) ? (type === 'percentage' ? `${number(value)}% of charges` : usd.format(Number(value))) : 'Not available';
const percent = value => present(value) ? `${Number(value).toFixed(2)}%` : 'Not available';
const ratio = value => present(value) ? `${Number(value).toFixed(2)}×` : '–';
const MAX_COMPARE = 6;
const state = {active:'overview', pages:{rates:1, plans:1, benchmarks:1, tins:1, tree:1}, totals:{}, rates:new Map(),
  compare:new Map(), running:false, runId:null, offset:0, poll:null, versions:{}, exports:{}, facets:null, tin:null, spread:null};

async function api(path, options = {}) {
  const response = await fetch(path, {cache:'no-store', ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
function message(text = '') { $('message').textContent = text; $('message').hidden = !text; }
function fail(error) { message(error.message || String(error)); }
function formQuery(id) {
  const query = new URLSearchParams();
  for (const [k, v] of new FormData($(id))) if (String(v).trim()) query.set(k, String(v).trim());
  return query;
}
function empty(text = 'No results available yet.') { return `<p class="muted empty-state">${esc(text)}</p>`; }
function table(columns, rows) {
  if (!rows.length) return empty();
  return `<div class="table-responsive"><table><thead><tr>${columns.map(c=>`<th>${esc(c[0])}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${columns.map(c=>`<td>${esc(typeof c[1] === 'function' ? c[1](r) : r[c[1]])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
const STATUS_CLASS = {CONFIRMED_PPO:'st-confirmed', PPO_CANDIDATE:'st-candidate', UNRESOLVED:'st-unresolved', CONFIRMED_NON_PPO:'st-non'};
const STATUS_LABEL = {CONFIRMED_PPO:'Confirmed PPO', PPO_CANDIDATE:'PPO candidate', UNRESOLVED:'Unresolved', CONFIRMED_NON_PPO:'Not PPO'};
const statusPill = s => s ? `<span class="status-tag ${STATUS_CLASS[s] || ''}" title="${esc(s)}">${esc(STATUS_LABEL[s] || s)}</span>` : '<span class="muted">–</span>';

/* Horizontal bars: one hue for magnitude, status colours only for network status. Labels and values in text ink. */
function bars(values, {format = number, status = false, total = null} = {}) {
  const entries = Object.entries(values || {});
  if (!entries.length) return empty();
  const max = Math.max(...entries.map(([, v]) => Number(v) || 0), 1);
  const sum = total ?? entries.reduce((a, [, v]) => a + (Number(v) || 0), 0);
  return `<div class="hbars" role="list">${entries.map(([k, v]) => {
    const share = sum ? ` · ${(100 * v / sum).toFixed(1)}%` : '';
    return `<div class="hbar-row" role="listitem" title="${esc(k)}: ${esc(format(v))}${esc(share)}">
      <span class="hbar-label">${status ? statusPill(k) : esc(k)}</span>
      <span class="hbar-track"><span class="hbar-fill ${status ? STATUS_CLASS[k] || '' : ''}" style="width:${100 * (Number(v) || 0) / max}%"></span></span>
      <span class="hbar-value">${esc(format(v))}<small>${esc(share)}</small></span></div>`;
  }).join('')}</div>`;
}
function kpis(id, cards) {
  $(id).innerHTML = cards.map(([label, value, note]) => `<article class="neu-card kpi-card"><p class="kpi-title">${esc(label)}</p><div class="kpi-value-row"><span class="kpi-number">${esc(value)}</span></div>${note ? `<p class="kpi-subtext">${esc(note)}</p>` : ''}</article>`).join('');
}
function updateExports() {
  document.querySelectorAll('[data-export]').forEach(button => { button.disabled = state.running || !state.exports[button.dataset.export]; });
}

/* ---------- Overview ---------- */
async function loadOverview() {
  const summary = await api('/api/summary');
  const m = summary.metrics;
  kpis('kpis', [
    ['Index plan / file rows', number(m.totalIndexPlans)], ['California group plans', number(m.caGroupPlans)],
    ['PPO plans', number(m.ppoPlans)], ['NPIs with a verified address', percent(m.addressVerificationPct)],
    ['Confirmed PPO rate rows', number(m.confirmedRows), 'Deliverable 1'], ['Candidate rate rows', number(m.candidateRows), 'Not confirmed PPO'],
  ]);
  $('pairStatus').innerHTML = Object.keys(m.pairNetworkStatus || {}).length ? bars(m.pairNetworkStatus, {status:true}) : empty('Run C4 to decide network status.');
  const scores = Object.entries(m.fileScores || {});
  $('overviewQuality').innerHTML = scores.length ? table([['File', r => r.file.split('/').pop()], ['Score', 'score'], ['Grade', 'grade'], ['Status', 'status']], scores.map(([file, r]) => ({file, ...r}))) : empty('C3 has not produced quality scores.');
  $('planDistribution').innerHTML = bars(m.planTypes);
  const tierOrder = ['Highly cost-effective', 'Cost-effective', 'At market', 'Above market', 'High cost'];
  const tiers = Object.entries(m.costTiers || {}).sort((a, b) => (tierOrder.indexOf(a[0]) + 1 || 99) - (tierOrder.indexOf(b[0]) + 1 || 99));
  $('costTiers').innerHTML = bars(Object.fromEntries(tiers));
  $('stageReports').innerHTML = table([['Stage', 'stage'], ['Last completed', 'finished']], Object.entries(summary.finished).map(([stage, finished]) => ({stage:stage.toUpperCase(), finished:finished || 'Not run'})));
  $('dataState').textContent = !summary.available ? 'No pipeline results yet. Open Run pipeline to start processing.' : 'Showing available pipeline outputs. Stage dates identify the last completed reports.';
}

/* ---------- Shared facets ---------- */
function fillSelect(select, values, label, labels = {}) {
  const previous = select.value;
  select.innerHTML = (label ? `<option value="">${esc(label)}</option>` : '') + values.map(v => `<option value="${esc(v)}">${esc(labels[v] ? `${v} — ${labels[v]}` : v)}</option>`).join('');
  if (values.includes(previous)) select.value = previous;
}
async function loadFacets() {
  const f = state.facets = await api('/api/rates/facets');
  fillSelect($('filterCode'), f.codes, 'All codes', f.codeLabels);
  fillSelect($('filterSpecialty'), f.specialties, 'All specialties');
  fillSelect($('filterRegion'), f.regions, 'All regions');
  fillSelect($('filterStatus'), f.statuses, 'All statuses');
  fillSelect($('filterTier'), f.tiers, 'All tiers');
  fillSelect($('spreadCode'), f.codes, '', f.codeLabels);
  fillSelect($('spreadStatus'), f.statuses, 'All statuses');
  fillSelect($('compareStatus'), f.statuses, 'Every provider in the rate file');
}

/* ---------- Pagination ---------- */
function pager(name, data) {
  state.pages[name] = data.page;
  const pages = Math.max(1, Math.ceil(data.total / data.pageSize));
  $(`${name}Page`).textContent = `Page ${data.page} of ${pages} · ${number(data.total)} ${name === 'tins' ? 'TINs' : name === 'tree' ? 'NPIs' : 'rows'}`;
  document.querySelector(`[data-page="${name}:-1"]`).disabled = data.page <= 1;
  document.querySelector(`[data-page="${name}:1"]`).disabled = data.page >= pages;
}

/* ---------- Compare selection ---------- */
const pairKey = r => `${r.npi}:${r.tin}`;
function saveCompare() {
  try { localStorage.setItem('ptd-compare', JSON.stringify([...state.compare.values()])); } catch (e) { /* storage unavailable */ }
}
function restoreCompare() {
  try { for (const p of JSON.parse(localStorage.getItem('ptd-compare') || '[]').slice(0, MAX_COMPARE)) state.compare.set(pairKey(p), p); } catch (e) { /* ignore */ }
}
function toggleCompare(p, on) {
  const key = pairKey(p);
  if (on && !state.compare.has(key)) {
    if (state.compare.size >= MAX_COMPARE) { message(`Compare at most ${MAX_COMPARE} providers at once.`); return false; }
    state.compare.set(key, {npi:p.npi, tin:p.tin, name:p.name || ''});
  } else if (!on) state.compare.delete(key);
  saveCompare(); renderCompareBar();
  return true;
}
function renderCompareBar() {
  const n = state.compare.size;
  $('compareBar').hidden = !n || state.active === 'compare';
  $('compareBarText').textContent = `${n} provider${n === 1 ? '' : 's'}`;
  $('compareBadge').hidden = !n; $('compareBadge').textContent = n;
  document.querySelectorAll('[data-compare]').forEach(el => { el.checked = state.compare.has(el.dataset.compare); });
}
const compareBox = r => `<input type="checkbox" class="compare-checkbox" data-compare="${esc(pairKey(r))}" data-npi="${esc(r.npi)}" data-tin="${esc(r.tin)}" data-name="${esc(r.name || '')}" aria-label="Compare ${esc(r.name || r.npi)} (TIN ${esc(r.tin)})" ${state.compare.has(pairKey(r)) ? 'checked' : ''}>`;

/* ---------- Rate explorer ---------- */
function variant(r) { return [r.negotiated_type, r.billing_class, r.setting, r.pos && `POS ${r.pos}`, r.modifier && `mod ${r.modifier}`].filter(Boolean).join(' · '); }
async function loadRates() {
  const version = state.versions.rates = (state.versions.rates || 0) + 1;
  const query = formQuery('rateFilters');
  query.set('page', state.pages.rates); query.set('pageSize', '25');
  const data = await api(`/api/rates?${query}`);
  if (version !== state.versions.rates) return;
  state.rates = new Map(data.rates.map(r => [r.id, r]));
  $('rateCount').textContent = `${number(data.total)} matching practice-site rate rows`;
  $('ratesBody').innerHTML = data.rates.length ? data.rates.map(r => `<tr>
    <td class="compare-checkbox-cell">${compareBox(r)}</td>
    <td><strong>${esc(r.name || 'Name unavailable')}</strong><br><span class="mono">NPI ${esc(r.npi)}</span><br><span class="mono">TIN ${esc(r.tin)}</span></td>
    <td>${esc(r.address || 'Address unavailable')}${present(r.address_rank) && r.address ? ` <span class="rank-chip" title="Site rank ${esc(r.address_rank)} for this NPI + TIN">#${esc(r.address_rank)}</span>` : ''}<br><small>${esc(r.verified === null ? 'Verification unavailable' : r.verified ? 'Verified by C5' : 'Not independently verified')}</small></td>
    <td><strong>${esc(r.code_type)} ${esc(r.code)}</strong>${r.description ? `<br><small>${esc(r.description)}</small>` : ''}<br><small>${esc(variant(r))}</small></td>
    <td>${esc(r.specialty || 'Specialty unavailable')}<br><small>${esc(r.region || '')}</small></td>
    <td class="rate-cell-val">${esc(money(r.rate, r.negotiated_type))}</td>
    <td>${present(r.cost_index) ? `${esc(Number(r.cost_index).toFixed(3))}×` : 'Not scored'}<br>${esc(r.tier || '')}<br><small>${esc(r.score_confidence ? `Confidence: ${r.score_confidence}` : '')}</small></td>
    <td>${statusPill(r.network_status)}</td>
    <td><button class="neu-btn neu-btn-sm" data-inspect="${esc(r.id)}">Inspect</button><br><button class="link-btn" data-spread="${esc(r.code)}">Price spread</button></td>
  </tr>`).join('') : '<tr><td colspan="9">No rates match these filters.</td></tr>';
  pager('rates', data);
}
function inspect(id) {
  const r = state.rates.get(id);
  if (!r) return;
  const values = {'Provider':r.name || 'Unavailable', 'NPI':r.npi, 'TIN':r.tin, 'Practice address':r.address || 'Unavailable', 'Site rank':r.address_rank,
    'Specialty':r.specialty, 'Specialty source':r.specialty_source, 'Region':r.region, 'Code / price variant':`${r.code_type} ${r.code} · ${variant(r)}`, 'Code description':r.description,
    'Negotiated rate':money(r.rate, r.negotiated_type), 'Network status':r.network_status, 'Network evidence':r.network_basis || 'Unavailable',
    'Address verified':r.verified === null ? 'Unavailable' : r.verified ? 'Yes' : 'No', 'Address confidence':r.address_confidence,
    'Address sources':r.address_sources, 'Address evidence':r.address_evidence, 'C7 cost index':r.cost_index,
    'Cost tier':r.tier || 'Not scored', 'Score confidence':r.score_confidence || 'Unavailable', 'NPI deactivated':r.deactivated === null ? 'Unavailable' : r.deactivated ? 'Yes' : 'No'};
  $('providerDetails').innerHTML = `<dl class="metric-list">${Object.entries(values).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(present(v) ? v : 'Unavailable')}</dd>`).join('')}</dl>`;
  $('providerDialog').showModal();
}

/* ---------- Compare providers ---------- */
async function loadCompare() {
  renderCompareBar();
  if (!state.compare.size) { $('compareResult').innerHTML = empty('Tick providers in the rate explorer or the price spread, or add an NPI above.'); return; }
  const query = new URLSearchParams({pairs:[...state.compare.keys()].join(',')});
  if ($('compareStatus').value) query.set('status', $('compareStatus').value);
  const data = await api(`/api/compare?${query}`);
  const keys = data.providers.map(p => p.key);
  const cards = data.providers.map(p => `<article class="neu-card compare-card-neu">
      <div class="card-header-clean"><strong>${esc(p.name || 'Name unavailable')}</strong><button class="neu-btn neu-btn-sm" data-uncompare="${esc(p.key)}" aria-label="Remove ${esc(p.name || p.npi)}">Remove</button></div>
      <p class="mono">NPI ${esc(p.npi)} · TIN ${esc(p.tin)}</p>
      <p>${statusPill(p.network_status)}</p>
      <p><small>${esc(p.specialty || 'Specialty unavailable')} · ${esc(p.region || 'Region unavailable')}</small></p>
      <p><small>${esc(p.address || 'Address unavailable')}${p.sites > 1 ? ` (+${p.sites - 1} more site${p.sites > 2 ? 's' : ''})` : ''}</small></p>
      <p>C7 cost index <strong>${p.cost_index != null ? esc(Number(p.cost_index).toFixed(3)) + '×' : 'not scored'}</strong> ${p.tier ? `· ${esc(p.tier)}` : ''}</p>
      ${data.basket[p.key] != null ? `<p>Basket of ${data.commonCodes} shared code${data.commonCodes === 1 ? '' : 's'}: <strong>${esc(money(data.basket[p.key]))}</strong></p>` : ''}
    </article>`).join('');
  let banner = '';
  const basket = Object.entries(data.basket);
  if (basket.length > 1) {
    basket.sort((a, b) => a[1] - b[1]);
    const name = k => { const p = data.providers.find(x => x.key === k); return p.name || `NPI ${p.npi}`; };
    const save = basket[basket.length - 1][1] - basket[0][1];
    banner = `<div class="savings-highlight-callout"><div class="savings-callout-text">For the ${data.commonCodes} billing code${data.commonCodes === 1 ? '' : 's'} (same price variant) that all selected providers bill, <strong>${esc(name(basket[0][0]))}</strong> costs <strong>${esc(money(basket[0][1]))}</strong>, ${esc(money(save))} (${(100 * save / basket[basket.length - 1][1]).toFixed(1)}%) less than <strong>${esc(name(basket[basket.length - 1][0]))}</strong>.</div></div>`;
  } else if (keys.length > 1) banner = `<p class="notice">The selected providers share no billing code and price variant, so no basket total can be compared.</p>`;
  const head = `<tr><th>Code / variant</th><th>Market median (n)</th>${data.providers.map(p => `<th>${esc(p.name || p.npi)}<br><small class="mono">${esc(p.tin)}</small></th>`).join('')}<th>Spread</th></tr>`;
  const body = data.rows.map(r => {
    const max = Math.max(...Object.values(r.rates));
    return `<tr><td><strong>${esc(r.code_type)} ${esc(r.code)}</strong>${r.description ? `<br><small>${esc(r.description)}</small>` : ''}<br><small>${esc(variant(r))}</small></td>
      <td>${esc(money(r.market.median))}<br><small>${esc(number(r.market.n))} providers</small></td>
      ${keys.map(k => {
        const v = r.rates[k];
        if (v == null) return '<td class="muted">Not billed</td>';
        const vs = r.market.median ? v / r.market.median : null;
        return `<td class="${v === r.cheapest && Object.keys(r.rates).length > 1 ? 'cell-best' : ''}" title="${esc(money(v))}${vs ? ` · ${ratio(vs)} the market median` : ''}">
          <strong>${esc(money(v, r.negotiated_type))}</strong><span class="cell-bar"><span style="width:${100 * v / max}%"></span></span><small>${vs ? `${ratio(vs)} median` : ''}</small></td>`;
      }).join('')}
      <td>${Object.keys(r.rates).length > 1 ? esc(ratio(r.spread)) : '–'}</td></tr>`;
  }).join('');
  $('compareResult').innerHTML = (data.missing.length ? `<p class="notice">Not in the rate file: ${esc(data.missing.join(', '))}</p>` : '') + banner +
    `<div class="compare-grid-layout">${cards}</div>` +
    `<article class="neu-card section-gap"><h3>Rates code by code</h3><p class="muted">Cheapest rate in each row is highlighted. “× median” compares with the median rate of every provider for that code and variant${data.status ? ` with status ${esc(data.status)}` : ''}.</p><div class="table-responsive"><table class="compare-matrix"><thead>${head}</thead><tbody>${body}</tbody></table></div></article>`;
}
async function addCompare(event) {
  event.preventDefault();
  const form = new FormData($('compareForm'));
  const npi = String(form.get('npi')).trim(), tin = String(form.get('tin') || '').trim();
  const data = await api(`/api/rates?${new URLSearchParams({npi, ...(tin ? {tin} : {}), pageSize:'200'})}`);
  const pairs = new Map(data.rates.map(r => [pairKey(r), r]));
  if (!pairs.size) { message(`No rates for NPI ${npi}${tin ? ` under TIN ${tin}` : ''}.`); return; }
  for (const r of pairs.values()) if (!toggleCompare(r, true)) break;
  $('compareForm').reset();
  await loadCompare();
}

/* ---------- Price spread ---------- */
function histogramSvg(h, stats, width) {
  const W = Math.max(320, Math.round(width)), H = 220, pad = {l:40, r:16, t:26, b:30};
  const max = Math.max(...h.map(b => b.count), 1), lo = h[0].from, hi = h[h.length - 1].to;
  const x = v => pad.l + (hi > lo ? (v - lo) / (hi - lo) : .5) * (W - pad.l - pad.r);
  const bw = (W - pad.l - pad.r) / h.length;
  const y = c => H - pad.b - c / max * (H - pad.t - pad.b);
  const marks = [['Q1', stats.q1], ['Median', stats.median], ['Q3', stats.q3]].map(([l, v]) => `<g><line class="chart-ref" x1="${x(v)}" x2="${x(v)}" y1="${pad.t}" y2="${H - pad.b}"/><text class="chart-label" x="${x(v)}" y="${pad.t - 8}" text-anchor="middle">${l} ${money(v)}</text></g>`).join('');
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Distribution of ${stats.n} provider rates from ${money(stats.min)} to ${money(stats.max)}, median ${money(stats.median)}">
    <line class="chart-axis" x1="${pad.l}" x2="${W - pad.r}" y1="${H - pad.b}" y2="${H - pad.b}"/>
    ${h.map((b, i) => `<rect class="chart-bar" x="${pad.l + i * bw + 1}" width="${Math.max(1, bw - 2)}" y="${y(b.count)}" height="${H - pad.b - y(b.count)}" rx="3"><title>${money(b.from)} – ${money(b.to)}: ${b.count} provider${b.count === 1 ? '' : 's'}</title></rect>`).join('')}
    ${marks}
    <text class="chart-label" x="${pad.l}" y="${H - 8}" text-anchor="start">${money(lo)}</text><text class="chart-label" x="${W - pad.r}" y="${H - 8}" text-anchor="end">${money(hi)}</text>
    <text class="chart-label" x="${pad.l - 6}" y="${pad.t + 4}" text-anchor="end">${max}</text><text class="chart-label" x="${pad.l - 6}" y="${H - pad.b}" text-anchor="end">0</text>
  </svg>`;
}
async function loadSpread(keepVariant = true) {
  const code = $('spreadCode').value;
  if (!code) { $('spreadResult').innerHTML = empty('No billing codes in the rate file yet.'); return; }
  const query = new URLSearchParams({code});
  if (keepVariant && $('spreadVariant').value) query.set('variant', $('spreadVariant').value);
  if ($('spreadStatus').value) query.set('status', $('spreadStatus').value);
  const data = state.spread = await api(`/api/rates/spread?${query}`);
  $('spreadVariant').innerHTML = data.variants.map(v => `<option value="${esc(v.key)}">${esc(variant(v) || 'Base rate')} · ${number(v.providers)} providers</option>`).join('');
  if (data.variant) $('spreadVariant').value = data.variant.key;
  if (!data.providers.length) { $('spreadResult').innerHTML = empty('No providers bill this code with these filters.'); return; }
  const s = data.stats, max = s.max || 1;
  const tiles = [['Providers', number(s.n)], ['Lowest', money(s.min)], ['Q1', money(s.q1)], ['Median', money(s.median)], ['Q3', money(s.q3)], ['Highest', money(s.max)], ['Highest ÷ lowest', s.min ? ratio(s.max / s.min) : '–']];
  const list = data.providers.map(p => `<div class="rank-row">
      <span>${compareBox(p)}</span>
      <span class="rank-name"><strong>${esc(p.name || 'Name unavailable')}</strong><small class="mono"> NPI ${esc(p.npi)} · TIN ${esc(p.tin)}</small><br><small>${esc(p.specialty || '')}${p.region ? ` · ${esc(p.region)}` : ''}</small></span>
      <span>${statusPill(p.network_status)}</span>
      <span class="hbar-track" title="${esc(money(p.rate))} · ${esc(ratio(p.vs_median))} the median"><span class="hbar-fill ${STATUS_CLASS[p.network_status] || ''}" style="width:${100 * p.rate / max}%"></span><span class="median-tick" style="left:${100 * s.median / max}%"></span></span>
      <span class="hbar-value">${esc(money(p.rate))}<small> ${esc(ratio(p.vs_median))}</small></span></div>`).join('');
  $('spreadResult').innerHTML = `<div class="stat-grid">${tiles.map(([l, v]) => `<article class="neu-card kpi-card"><p class="kpi-title">${esc(l)}</p><div class="kpi-value-row"><span class="kpi-number">${esc(v)}</span></div></article>`).join('')}</div>
    <article class="neu-card section-gap"><h3>${esc(data.code)}${data.description ? ` · ${esc(data.description)}` : ''}: distribution of provider rates</h3>${histogramSvg(data.histogram, s, $('spreadResult').clientWidth - 64)}</article>
    <article class="neu-card section-gap"><h3>Providers ranked by rate</h3><p class="muted">Bar colour is the network status; the thin line marks the median. Tick providers to compare them across all their codes.${data.truncated ? ` Showing the cheapest and most expensive ${number(data.providers.length)} of ${number(s.n)}.` : ''}</p><div class="rank-list">${list}</div></article>`;
}

/* ---------- TIN -> NPI -> address ---------- */
async function loadEntities() {
  const o = await api('/api/c5/overview');
  kpis('c5Kpis', [['TINs', number(o.tins)], ['NPIs', number(o.npis)], ['NPI + TIN pairs', number(o.pairs)], ['Distinct practice sites', number(o.sites)],
    ['Pairs with an address', o.pairs ? `${(100 * o.pairsWithAddress / o.pairs).toFixed(1)}%` : '–'], ['Pairs with a verified best site', o.pairs ? `${(100 * o.pairsVerified / o.pairs).toFixed(1)}%` : '–']]);
  $('c5NpisPerTin').innerHTML = bars(o.npisPerTin);
  $('c5SitesPerPair').innerHTML = bars(o.sitesPerPair);
  $('c5TinsPerNpi').innerHTML = bars(o.tinsPerNpi);
  await loadTins();
}
async function loadTins() {
  const query = formQuery('entitySearch'); query.set('page', state.pages.tins);
  const data = await api(`/api/c5/search?${query}`);
  state.highlightNpi = data.npi;
  $('tinList').innerHTML = data.rows.length ? `<div class="tin-list">${data.rows.map(r => `<button class="tin-item ${r.tin === state.tin ? 'active' : ''}" data-tin="${esc(r.tin)}">
      <span class="mono"><strong>TIN ${esc(r.tin)}</strong></span><span>${esc(r.org_name || 'Organisation name unavailable')}</span>
      <small>${number(r.npis)} NPI${r.npis === 1 ? '' : 's'} · ${number(r.sites)} site${r.sites === 1 ? '' : 's'} · ${number(r.verified_pairs)} verified</small></button>`).join('')}</div>` : empty('No TIN matches this search.');
  pager('tins', data);
  if (data.rows.length && (!state.tin || data.q)) await loadTree(data.rows[0].tin);
}
async function loadTree(tin) {
  if (tin && tin !== state.tin) { state.tin = tin; state.pages.tree = 1; }
  document.querySelectorAll('.tin-item').forEach(b => b.classList.toggle('active', b.dataset.tin === state.tin));
  const data = await api(`/api/c5/tin?${new URLSearchParams({tin:state.tin, page:state.pages.tree, pageSize:'25'})}`);
  const npis = data.rows.map(n => `<div class="tree-npi ${n.npi === state.highlightNpi ? 'highlight' : ''}">
      <div class="tree-node npi-node">
        <strong>${esc(n.name || 'Name unavailable')}</strong><span class="mono">NPI ${esc(n.npi)}</span>
        <small>${n.entity_type === '2' ? 'Organisation (Type 2)' : n.entity_type === '1' ? 'Individual (Type 1)' : 'Type unknown'}${n.taxonomy ? ` · ${esc(n.taxonomy)}` : ''}${n.deactivated ? ' · deactivated in NPPES' : ''}</small>
        <span title="${esc(n.network_basis || '')}">${statusPill(n.network_status)}</span>
        <small>${number(n.rate_rows)} rate rows over ${number(n.codes)} codes${n.other_tins ? ` · also bills under ${n.other_tins} other TIN${n.other_tins === 1 ? '' : 's'}` : ''}</small>
        ${n.rate_rows ? `<span><button class="link-btn" data-rates-npi="${esc(n.npi)}" data-rates-tin="${esc(data.tin)}">See rates</button> · <label>${compareBox({npi:n.npi, tin:data.tin, name:n.name})} compare</label></span>` : ''}
      </div>
      <div class="tree-sites">${n.sites.length ? n.sites.map(s => `<div class="tree-node site-node ${s.rank === 1 ? 'best' : ''}">
          <span class="rank-chip" title="Site rank ${s.rank}: ${s.rank === 1 ? 'the most confident site for this NPI + TIN' : 'an extra site kept because its confidence is close to the best'}">#${esc(s.rank)}</span>
          <strong>${esc(s.address)}</strong>
          <small>${esc(s.county ? `${s.county} County` : '')}</small>
          <span class="conf"><span class="hbar-track"><span class="hbar-fill" style="width:${100 * (s.confidence || 0)}%"></span></span><small>confidence ${esc(Number(s.confidence || 0).toFixed(2))} · ${s.verified ? '✔ verified' : 'not verified'}</small></span>
          <small title="${esc(s.evidence || '')}">Sources: ${esc((s.sources || '').split('|').join(', '))}</small></div>`).join('') : '<div class="tree-node site-node muted">No address found in any source</div>'}</div>
    </div>`).join('');
  $('tinTree').innerHTML = `<div class="tree-root"><div class="tree-node tin-node"><span class="badge-category">TIN</span><strong class="mono">${esc(data.tin)}</strong><span>${esc(data.org_name || 'Organisation name unavailable')}</span><small>${number(data.npis)} NPIs · ${number(data.sites)} distinct practice sites</small></div></div><div class="tree-branches">${npis}</div>`;
  pager('tree', data);
}

/* ---------- Other tables ---------- */
async function loadPlans() {
  const query = formQuery('planFilters'); query.set('page', state.pages.plans);
  const data = await api(`/api/c2/plans?${query}`);
  $('plansTable').innerHTML = data.available ? table([['Plan', 'plan_name'], ['Plan ID', 'plan_id'], ['Market', 'market_type'], ['Scope', 'in_scope_ppo'], ['Network', 'final_network_type'], ['Confidence', 'confidence'], ['Decision rule', 'decided_by'], ['Reason', 'reason']], data.rows) : empty('Run C2 to generate plan classifications.');
  pager('plans', data);
}
async function loadBenchmarks() {
  const query = formQuery('benchmarkFilters'); query.set('page', state.pages.benchmarks);
  const data = await api(`/api/benchmarks?${query}`);
  $('benchmarksTable').innerHTML = data.available ? table([
    ['NPI', 'npi'], ['TIN', 'tin'], ['Specialty', 'specialty'], ['Region', 'region'], ['Code / variant', r => `${r.code} · ${variant(r)}`],
    ['Rate', r => money(r.rate, r.negotiated_type)], ['Peer Q1', r => money(r.peer_q1)], ['Peer median', r => money(r.peer_median)], ['Peer Q3', r => money(r.peer_q3)], ['Peers', 'peer_n'], ['Rate / median', 'rate_index'], ['Peer level', 'peer_level'],
  ], data.rows) : empty('Run C7 for the selected cohort to generate peer benchmarks.');
  pager('benchmarks', data);
}
async function loadQuality() {
  const data = await api('/api/c3/metrics');
  $('qualityScores').innerHTML = table([['File', r => r.rate_file_url?.split('/').pop()], ['Bucket', 'bucket'], ['Score', 'score'], ['Grade', 'grade'], ['Status', 'status'], ['Sampled', 'sampled']], data.scores);
  $('qualityMetrics').innerHTML = table([['File', r => r.rate_file_url?.split('/').pop()], ['Bucket', 'bucket'], ['Metric', 'metric'], ['Measured value', 'value'], ['Score (0–1)', 'score'], ['Weight', 'weight'], ['Measured', 'measured'], ['Justification', 'justification']], data.metrics);
}
const loaders = {overview:loadOverview, rates:loadRates, compare:loadCompare, spread:() => loadSpread(true), entities:loadEntities,
  plans:loadPlans, benchmarks:loadBenchmarks, quality:loadQuality, pipeline:configureRun};
const pageLoaders = {rates:loadRates, plans:loadPlans, benchmarks:loadBenchmarks, tins:loadTins, tree:() => loadTree()};
async function showTab(name) {
  state.active = name;
  document.querySelectorAll('.workspace-panel').forEach(panel => { panel.hidden = panel.id !== name; });
  document.querySelectorAll('[data-tab]').forEach(button => {
    button.classList.toggle('active', button.dataset.tab === name);
    if (button.dataset.tab === name) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  });
  renderCompareBar();
  if (!state.running || name === 'pipeline') await loaders[name]();
}

/* ---------- Pipeline run ---------- */
function updateRun(run) {
  if (run.id !== state.runId) {
    state.runId = run.id; state.offset = 0;
    $('runLogs').textContent = run.id ? `Command: ${run.command.join(' ')}\n\n` : 'No run started in this server session.';
  }
  if (run.logsTruncated) $('runLogs').textContent += '\n[Earlier log lines are available in the full log file.]\n';
  for (const line of run.logs || []) if (line.seq > state.offset) $('runLogs').textContent += line.text + '\n';
  state.offset = run.nextOffset || 0;
  if ($('runLogs').textContent.length > 300000) $('runLogs').textContent = $('runLogs').textContent.slice(-250000);
  $('runLogs').scrollTop = $('runLogs').scrollHeight;
  state.running = run.status === 'running';
  $('runButton').disabled = state.running;
  $('runStatus').textContent = run.status === 'idle' ? 'Idle' : run.status === 'running' ? 'Running…' : `${run.status === 'succeeded' ? 'Completed' : 'Failed'} · exit code ${run.exitCode}`;
  if (run.finished) $('runStatus').textContent += ` · ${new Date(run.finished).toLocaleString()}`;
  $('backendStatusText').textContent = state.running ? 'Pipeline running' : run.status === 'failed' ? 'Last run failed' : $('backendStatusText').textContent;
  updateExports();
}
async function pollRun() {
  clearTimeout(state.poll);
  try {
    const run = await api(`/api/pipeline/run?after=${state.offset}`);
    updateRun(run);
    if (state.running) state.poll = setTimeout(pollRun, 1000);
    else {
      if (run.status === 'failed') message(`Pipeline failed with exit code ${run.exitCode}. Open Run pipeline to inspect the actual error output.`);
      await refresh(false);
    }
  } catch (error) { fail(error); state.poll = setTimeout(pollRun, 2500); }
}
async function refresh(clearMessage = true) {
  if (clearMessage) message();
  const status = await api(`/api/status?after=${state.offset}`);
  state.exports = status.exports || {};
  $('pipelinePath').textContent = `Pipeline folder: ${status.dataDirectory.replace(/[\\/]data$/, '')} · Output folder: ${status.outputDirectory}`;
  $('backendStatusText').textContent = status.ratesReady ? 'Pipeline results loaded' : 'No C6 rate results yet';
  updateRun(status.run);
  if (state.running) { clearTimeout(state.poll); state.poll = setTimeout(pollRun, 1000); return; }
  await loadOverview();
  if (status.ratesReady) await loadFacets();
  if (state.active !== 'overview') await loaders[state.active]();
}
async function configureRun() {
  const config = await api('/api/pipeline/config');
  for (const id of ['fromStage', 'toStage']) if (!$(id).options.length) $(id).innerHTML = config.stages.map(s => `<option value="${esc(s)}">${esc(s.toUpperCase())}</option>`).join('');
  if (!$('asOf').value) { $('toStage').value = 'c7'; $('asOf').value = config.as_of; }
  const i = config.inputs;
  $('repoInputs').innerHTML = `<dl class="metric-list">
    <dt>Folder</dt><dd class="mono">${esc(i.inputs_dir)}</dd>
    <dt>HiLabs workbook</dt><dd>${i.workbook ? `✔ ${esc(i.workbook.split(/[\\/]/).pop())}` : 'Not found: every billing code is extracted and C7 weights codes equally.'}</dd>
    <dt>Saved PPO directory check</dt><dd>${i.ppo_check_csv ? `✔ ${esc(i.ppo_check_csv.split(/[\\/]/).pop())}: ${esc(number(i.ppo_check_npis))} NPIs searched on the site (used as is; the directory site is not searched)${i.ppo_check_layout === 'legacy' ? '. This is the early ppo_check_results.csv layout, whose listings carry no TIN; a c4_ppo_check.csv is better.' : ''}` : i.live_directory_check ? 'Not found: C4 searches the Health Net directory live (resumable; can take hours on the whole file).' : `Not found, and the live search is unavailable (${esc(i.live_directory_check_unavailable)}). C4 confirms PPO only from other evidence.`}</dd>
    ${i.ignored_csv?.length ? `<dt>Ignored</dt><dd>${esc(i.ignored_csv.join(', '))}: not PPO check tables (they need the columns of c4_ppo_check.csv).</dd>` : ''}
  </dl><p class="muted">Put the files in this folder to change them. See inputs/README.md.</p>`;
}
async function startRun(event) {
  event.preventDefault(); message();
  const form = new FormData($('runForm')), options = {};
  for (const [key, value] of form.entries()) {
    if (key === 'fetch_public') options[key] = true;
    else if (key === 'workers') options[key] = Number(value);
    else if (key === 'rate_files') options[key] = String(value).split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    else if (String(value).trim()) options[key] = String(value).trim();
  }
  if (!options.rate_files?.length) delete options.rate_files;
  $('runButton').disabled = true;
  try {
    const run = await api('/api/pipeline/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(options)});
    updateRun(run); await showTab('pipeline'); state.poll = setTimeout(pollRun, 500);
  } catch (error) { $('runButton').disabled = state.running; throw error; }
}

/* ---------- Wiring ---------- */
function bindForm(id, name, loader) { $(id).addEventListener('submit', event => { event.preventDefault(); state.pages[name] = 1; loader().catch(fail); }); }
async function initialize() {
  restoreCompare();
  document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => { message(); showTab(button.dataset.tab).catch(fail); }));
  document.querySelectorAll('[data-goto]').forEach(button => button.addEventListener('click', () => showTab(button.dataset.goto).catch(fail)));
  $('refreshButton').addEventListener('click', () => refresh().catch(fail));
  bindForm('rateFilters', 'rates', loadRates); bindForm('planFilters', 'plans', loadPlans); bindForm('benchmarkFilters', 'benchmarks', loadBenchmarks);
  bindForm('entitySearch', 'tins', loadTins);
  $('resetRates').addEventListener('click', () => setTimeout(() => { state.pages.rates = 1; loadRates().catch(fail); }));
  $('spreadForm').addEventListener('submit', event => { event.preventDefault(); loadSpread(true).catch(fail); });
  $('spreadCode').addEventListener('change', () => loadSpread(false).catch(fail));
  $('spreadStatus').addEventListener('change', () => loadSpread(false).catch(fail));
  $('spreadVariant').addEventListener('change', () => loadSpread(true).catch(fail));
  $('compareForm').addEventListener('submit', event => addCompare(event).catch(fail));
  $('compareStatus').addEventListener('change', () => loadCompare().catch(fail));
  document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => {
    const [name, step] = button.dataset.page.split(':');
    state.pages[name] = Math.max(1, state.pages[name] + Number(step)); pageLoaders[name]().catch(fail);
  }));
  document.querySelectorAll('[data-export]').forEach(button => button.addEventListener('click', async () => {
    try {
      const response = await fetch(`/api/export?kind=${encodeURIComponent(button.dataset.export)}`);
      if (!response.ok) throw new Error((await response.json()).error);
      const url = URL.createObjectURL(await response.blob()), a = document.createElement('a');
      a.href = url; a.download = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] || 'pipeline-export';
      a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { fail(error); }
  }));
  document.addEventListener('change', event => {
    const input = event.target;
    if (!input.matches('[data-compare]')) return;
    if (!toggleCompare({npi:input.dataset.npi, tin:input.dataset.tin, name:input.dataset.name}, input.checked)) input.checked = false;
    if (state.active === 'compare') loadCompare().catch(fail);
  });
  document.addEventListener('click', event => {
    const el = event.target.closest('[data-inspect],[data-spread],[data-uncompare],[data-tin],[data-rates-npi]');
    if (!el) return;
    if (el.dataset.inspect) inspect(el.dataset.inspect);
    else if (el.dataset.spread) { $('spreadCode').value = el.dataset.spread; $('spreadVariant').innerHTML = ''; showTab('spread').catch(fail); }
    else if (el.dataset.uncompare) { state.compare.delete(el.dataset.uncompare); saveCompare(); loadCompare().catch(fail); }
    else if (el.dataset.ratesNpi) {
      $('rateFilters').reset(); $('rateFilters').elements.npi.value = el.dataset.ratesNpi; $('rateFilters').elements.tin.value = el.dataset.ratesTin;
      state.pages.rates = 1; showTab('rates').catch(fail);
    } else if (el.dataset.tin && el.classList.contains('tin-item')) loadTree(el.dataset.tin).catch(fail);
  });
  $('clearCompare').addEventListener('click', () => { state.compare.clear(); saveCompare(); renderCompareBar(); });
  $('closeDialog').addEventListener('click', () => $('providerDialog').close());
  $('runForm').addEventListener('submit', event => startRun(event).catch(fail));
  await configureRun(); await refresh();
}
document.addEventListener('DOMContentLoaded', () => initialize().catch(fail));
