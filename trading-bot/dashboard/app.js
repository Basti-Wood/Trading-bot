'use strict';
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = value => value == null ? '—' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(value);
const percent = value => value == null ? '—' : `${value > 0 ? '+' : ''}${(value*100).toFixed(2)}%`;
const time = value => value ? new Date(value).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
const signal = value => `<span class="pill ${{BUY:'buy',SELL:'sell',HOLD:'hold',WAIT:'hold',BULLISH:'buy',BEARISH:'sell',NEUTRAL:'hold'}[value] || ''}">${escape(value)}</span>`;
const outlook = value => signal({BUY:'BULLISH',SELL:'BEARISH',HOLD:'NEUTRAL'}[value] || value || 'UNTRAINED');
function positionSummary(position) {
  if(!position) return 'Position data is not available yet.';
  if(!position.qty) return 'Position: 0 shares. Waiting does not open a position.';
  const owner=position.owned?'this strategy':position.strategy==='external'?'external account position':`${position.strategy} strategy`;
  const entry=position.avg_entry_price==null?'':` / average entry ${money(Number(position.avg_entry_price))}`;
  const pnl=position.unrealized_pl==null?'':` / unrealized ${money(Number(position.unrealized_pl))}`;
  return `Position: ${position.qty} shares / ${owner}${entry}${pnl}`;
}
let state, selected, timer, selectedStrategy='', refreshSerial=0;
function toast(message) { $('toast').textContent=message; $('toast').hidden=false; clearTimeout(timer); timer=setTimeout(()=>$('toast').hidden=true,6500); }
function drawChart(items) {
  if(items.length<2) { $('chart').textContent='Collecting account snapshots. No simulated returns are displayed.'; return; }
  const points=[...items].reverse().map(x=>Number(x.equity));
  const lo=Math.min(...points), hi=Math.max(...points), range=hi-lo || Math.max(1,hi*.001);
  const path=points.map((v,i)=>`${i?'L':'M'}${(i/(points.length-1)*650+60).toFixed(1)},${(145-(v-lo)/range*110).toFixed(1)}`).join(' ');
  $('chart').innerHTML=`<svg viewBox="0 0 730 180" role="img" aria-label="Observed account equity over time"><line class="chart-grid" x1="60" x2="715" y1="35" y2="35"/><line class="chart-grid" x1="60" x2="715" y1="145" y2="145"/><text x="0" y="39">${escape(money(hi))}</text><text x="0" y="149">${escape(money(lo))}</text><path class="chart-line" d="${path}"/></svg>`;
}
function renderModels() {
  const item=state.symbols.find(x=>x.symbol===selected) || state.symbols[0];
  if(!item) {
    $('symbols').innerHTML='';
    $('verdict').textContent='Waiting for a complete market scan and a qualified shortlist.';
    for(const id of ['model-cards','news','decisions','validation','feedback-progress','feedback-scores','feedback-latest']) $(id).innerHTML='';
    $('feedback-note').textContent='Training begins after stocks qualify for the shortlist.';
    return;
  }
  selected=item.symbol;
  $('symbols').innerHTML=state.symbols.map(x=>`<button data-symbol="${escape(x.symbol)}" class="${x.symbol===selected?'active':''}" aria-pressed="${x.symbol===selected}">${escape(x.symbol)}</button>`).join('');
  const f=item.forecast, r=item.research;
  $('verdict').innerHTML=f ? `<div><span class="small muted">DECISION </span>${signal(f.action)}<h3>${escape(selected)} · ${escape(f.agreement)} / 3 forecast votes</h3><span class="small muted">Expected return ${percent(f.expected_return)} · indicative price ${money(f.last_close*(1+f.expected_return))} on latest completed close ${money(f.last_close)}</span></div><p>${escape(f.reason)}</p>` : `<div>${signal('WAITING')}<h3>${escape(selected)} is waiting for its first trained model.</h3></div><p>Training starts when Alpaca credentials and sufficient history are available.</p>`;
  $('verdict').insertAdjacentHTML('beforeend', `<div class="holding-context small muted">${escape(positionSummary(item.position))}</div>`);
  $('model-cards').innerHTML=(f?.models || [{name:'Ridge regression'},{name:'Extra Trees'},{name:'Histogram gradient boosting'}]).map((m,i)=>`<article class="model-card"><div class="top"><span class="number">FORECAST / 0${i+1}</span>${outlook(m.signal)}</div><h3>${escape(m.name)}</h3><div class="prediction ${(m.signal||'').toLowerCase()}">${percent(m.expected_return)}</div><span class="small muted">${escape(f?.horizon || "Waiting for a forecast")}</span><div class="drivers">${(m.drivers||[]).map(d=>`<div class="driver"><span>${escape((state.trading_mode==='daytrade'?d.feature.replace(/(\d+)d/g,'$1 bars'):d.feature).replaceAll('_',' '))}</span><span>${percent(d.impact)} impact</span></div>`).join('') || '<p class="small muted">Feature sensitivities appear after training.</p>'}</div><p class="small muted">${m.validation ? `Holdout net ${percent(m.validation.net_return)} · ${m.validation.active_days} active days` : 'No validation results yet'}</p></article>`).join('');
  const opinions=r?.opinions || [];
  $('model-cards').innerHTML += opinions.length ? opinions.map(o=>`<article class="model-card llm"><div><div class="top"><span class="number">NEWS ANALYST / FREELLM</span>${outlook(o.action)}</div><h3>${escape(o.model)}</h3><p class="small muted">Self-assessed confidence ${Math.round(o.confidence*100)}% · ${time(r.at)}</p><span class="small muted">Source IDs: ${o.source_ids.map(escape).join(', ') || 'none'}</span></div><div class="llm-rationale">${escape(o.rationale)}</div></article>`).join('') : `<article class="model-card llm"><div><span class="number">NEWS ANALYST / FREELLM</span><h3>Waiting for an independent opinion</h3></div><p class="muted">${escape(r?.message || state.research_status.status)}. New buys require fresh news evidence and agreement.</p></article>`;
  $('news').className='news-list';
  $('news').innerHTML=(r?.sources||[]).map(n=>{let url;try{url=new URL(n.url);}catch{return '';}return ['https:','http:'].includes(url.protocol)?`<div class="news-item"><a href="${escape(url.href)}" target="_blank" rel="noopener noreferrer">${n.id}. ${escape(n.title)} ↗</a><small>${escape(n.provider)} · ${time(n.published_at)}</small></div>`:'';}).join('') || '<p class="empty">No recent news sources available.</p>';
  $('decisions').innerHTML=state.events.filter(e=>e.symbol===selected).slice(0,8).map(e=>`<div class="timeline-item">${signal(e.action)} <time>${time(e.at)}</time><p>${escape(e.reason)}</p></div>`).join('') || '<p class="empty">No decisions recorded for this symbol.</p>';
  const m=item.model?.metrics;
  $('validation').innerHTML=[['Forecast holdout net',percent(m?.net_return)],['Maximum drawdown',percent(m?.max_drawdown)],['Buy & hold benchmark',percent(m?.benchmark_return)],['Trading eligibility',item.model ? item.model.eligible?'Passed':'Research only':'Untrained']].map(([k,v])=>`<div><span>${k}</span><strong>${escape(v)}</strong></div>`).join('');
  const feedback=item.feedback || {}, active=item.model?.feedback, candidate=item.candidate?.feedback;
  $('feedback-progress').innerHTML=[['Awaiting outcomes',feedback.pending||0],['Resolved examples',feedback.resolved||0],['Used by current model',active?.train_rows||0],['Used by latest candidate',candidate?.train_rows||0]].map(([k,v])=>`<div><span>${k}</span><strong>${escape(v)}</strong></div>`).join('');
  $('feedback-note').textContent=`${feedback.captured||0} forecasts recorded · ${feedback.excluded||0} excluded. ${candidate?.validation_rows||0} recent examples reserved for validation, ${candidate?.purged_rows||0} excluded at the split boundary. Resolved examples enter fitting after they pass beyond the latest ${state.settings.validation_bars} ${state.trading_mode==='daytrade'?'five-minute bars':'sessions'} and the boundary gap. A candidate must pass validation before replacing the current model.`;
  const names={ridge:'Ridge regression',extra_trees:'Extra Trees',hist_gradient:'Histogram gradient boosting',ensemble:'Forecast ensemble'};
  $('feedback-scores').innerHTML=Object.entries(feedback.scores||{}).map(([id,s])=>`<tr><td>${escape(names[id]||id)}</td><td>${escape(s.observations)}</td><td>${(s.mae*100).toFixed(3)} percentage points</td><td>${(s.direction_accuracy*100).toFixed(1)}%</td></tr>`).join('') || '<tr><td colspan="4" class="empty">Scores appear after the forecast horizon completes.</td></tr>';
  $('feedback-latest').innerHTML=(feedback.latest||[]).map(e=>`<div class="log-row"><time>${time(e.bar_at)}</time><span>Predicted ${percent(e.expected_return)} · observed ${percent(e.actual_return)} · model ${escape(e.model_version)}</span></div>`).join('');
}
function render(s) {
  state=s; selectedStrategy=s.trading_mode; selected ||= s.symbols[0]?.symbol;
  const parallel=s.portfolio_mode==='both', strategies=s.strategies||[];
  $('parallel-strategies').hidden=!parallel;
  $('strategy-cards').innerHTML=parallel?strategies.map(st=>`<article class="panel"><div class="section-title"><h3>${st.id==='daily'?'Daily investing':'Day trading'}</h3><span class="pill">${st.entries_enabled?'ENTRIES ENABLED':'ENTRIES PAUSED'}</span></div><p>${money(st.capital_ceiling)} capital allowance / ${Math.round(st.capital_fraction*100)}% share</p><p class="small muted">${escape(st.engine.status)} / training: ${escape(st.training.status)}</p><div class="strategy-actions"><button data-view="${escape(st.id)}" aria-pressed="${st.id===selectedStrategy}">${st.id===selectedStrategy?'Viewing forecasts':'View forecasts'}</button><button data-strategy="${escape(st.id)}" data-action="${st.entries_enabled?'pause':'resume'}" ${!s.configured||!s.settings.llm_configured?'disabled':''}>${st.entries_enabled?'Pause entries':'Enable entries'}</button></div></article>`).join(''):'';
  const intraday=s.trading_mode==='daytrade', universe=s.universe||{};
  $('strategy-label').textContent=intraday?'DAY TRADING':'DAILY';
  $('strategy-summary').textContent=intraday?`Five-minute bars / 15-minute forecasts. Risk polling every ${s.settings.poll_seconds}s; up to ${s.settings.daytrade_max_positions} positions. Timed exits after ${s.settings.daytrade_hold_minutes} minutes; flattening starts ${s.settings.daytrade_close_minutes} minutes before market close. Paper only.`:'Daily forecasts. Entry window 09:35-09:59 New York time.';
  $('scan-summary').textContent=s.settings.universe_mode==='market'?`${s.scan_status.status} / ${universe.total_assets||0} tradable listed assets / ${universe.covered||0} with snapshots / ${universe.qualified||0} passed filters / ${(universe.symbols||[]).length} shortlisted / ${time(universe.at)}${s.scan_status.message?' / '+s.scan_status.message:''}`:`Fixed watchlist / ${s.symbols.length} symbols`;
  $('scan-candidates').innerHTML=(universe.candidates||[]).map(c=>{const f=s.symbols.find(x=>x.symbol===c.symbol)?.forecast;return `<tr><td>${escape(c.symbol)}</td><td>${money(c.price)}</td><td>${percent(c.change)}</td><td>${money(c.dollar_volume)}</td><td>${percent(f?.expected_return)}</td><td>${signal(f?.action||'UNTRAINED')}</td></tr>`;}).join('') || '<tr><td colspan="6" class="empty">No market candidates available yet.</td></tr>';
  $('connection').textContent=(parallel?s.portfolio_engine?.status||s.engine.status:s.engine.status).toUpperCase();
  $('side-mode').textContent=`${s.mode.toUpperCase()} TRADING`; $('mode-label').textContent=s.mode.toUpperCase();
  $('equity').textContent=money(s.account?.equity); $('pnl').textContent=money(s.account?.daily_pnl);
  $('pnl').className=(s.account?.daily_pnl||0)<0?'sell':'buy';
  $('capital').textContent=money(s.settings.capital); $('allocation').textContent=`${Math.round(s.settings.gross_fraction*100)}% maximum gross exposure`;
  $('entry-state').textContent=parallel?`${strategies.filter(x=>x.entries_enabled).length} / 2 enabled`:s.entries_enabled?'Enabled':'Paused';
  $('toggle').textContent=parallel?(s.any_entries_enabled?'Pause both':'Enable both'):(s.entries_enabled?'Pause entries':`Enable ${s.mode} entries`); $('toggle').disabled=!s.configured||!s.settings.llm_configured;
  $('train').disabled=!s.configured||['running','starting'].includes(s.training.status);
  $('notice').textContent=!s.configured?'Setup needed: add Alpaca keys, your FreeLLM key, and a dashboard password to .env, then restart.':s.account?.loss_halt?'Daily loss limit reached. New entries are disabled; protective exits remain active.':`${(parallel?s.portfolio_engine?.message:null) || s.engine.message || s.engine.status} · ${s.settings.feed.toUpperCase()} data · ${s.account?.market_open?'Market open':'Market closed'} · ${s.settings.web_search?'Alpaca + Brave news search':'Alpaca news; optional Brave search not configured'}`;
  $('memory').textContent=`${s.resources.process_memory_mb.toFixed(0)} MB / 6 GB`; $('memory-bar').value=s.resources.process_memory_mb;
  $('train-memory').textContent=`${(s.resources.training_memory_mb/1024).toFixed(1)} GB`;
  $('training-status').textContent=`${s.training.status}${s.training.symbol?' · '+s.training.symbol:''}${s.training.model?' / '+s.training.model:''}`;
  $('training-log').innerHTML=s.training_history.slice(0,12).map(e=>`<div class="log-row"><time>${time(e.at)}</time><span>${escape(e.symbol||'')} ${escape(e.model||'')} · ${escape(e.message)}</span></div>`).join('');
  $('orders').innerHTML=s.orders.map(o=>`<tr><td>${time(o.at)}</td><td>${escape(o.symbol)}</td><td>${escape(o.strategy||'daily')}</td><td>${signal(o.side.toUpperCase())}</td><td>${escape(o.qty)}</td><td>${escape(o.filled_qty??'—')}</td><td>${o.filled_avg_price?money(Number(o.filled_avg_price)):'—'}</td><td>${escape(o.status)}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">No orders submitted.</td></tr>';
  $('positions').innerHTML=(s.account?.positions||[]).map(p=>`<div class="position"><b>${escape(p.symbol)}${p.strategy?` / ${escape(p.strategy)}`:''}</b><span>${escape(p.qty)} shares</span><span>${money(Number(p.market_value))}</span><span>${money(Number(p.unrealized_pl))} unrealized</span></div>`).join('') || 'No open positions.';
  $('updated').textContent=`LAST UPDATED ${time(s.at)}`;
  drawChart(s.equity); renderModels();
}
async function refresh() {const serial=++refreshSerial;try{const r=await fetch('/api/state'+(selectedStrategy?'?strategy='+encodeURIComponent(selectedStrategy):''));if(!r.ok)throw Error(`Dashboard request failed (${r.status})`);const snapshot=await r.json();if(serial===refreshSerial)render(snapshot);}catch(e){$('connection').textContent='OFFLINE';$('notice').textContent=e.message+' · displayed information may be stale.';}}
async function control(action,strategy='all') {try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-Bot-Control':'dashboard'},body:JSON.stringify({action,strategy})});const body=await r.json();if(!r.ok)throw Error(body.error||'Action failed');toast(action==='train'?'Retraining queued.':'Entry setting saved.');await refresh();}catch(e){toast(e.message);}}
$('toggle').addEventListener('click',()=>control((state.portfolio_mode==='both'?state.any_entries_enabled:state.entries_enabled)?'pause':'resume'));
$('train').addEventListener('click',()=>control('train',selectedStrategy||'all'));
$('symbols').addEventListener('click',e=>{const b=e.target.closest('[data-symbol]');if(b){selected=b.dataset.symbol;renderModels();}});
$('strategy-cards').addEventListener('click',e=>{const view=e.target.closest('[data-view]'), toggle=e.target.closest('[data-strategy]');if(view){selectedStrategy=view.dataset.view;selected=null;refresh();}else if(toggle){control(toggle.dataset.action,toggle.dataset.strategy);}});
refresh(); setInterval(refresh,5000);
