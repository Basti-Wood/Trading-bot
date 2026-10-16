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
  const votes=Object.values(f?.votes || {}), agreeing=Math.max(votes.filter(v=>v==='BUY').length, votes.filter(v=>v==='SELL').length);
  $('verdict').innerHTML=f ? `<div><span class="small muted">DECISION </span>${signal(f.action)}<h3>${escape(selected)} · ${votes.length ? `${agreeing} / ${votes.length} votes agree (${state.settings.min_votes} needed)` : `${escape(f.agreement)} / 3 forecast votes`}</h3><span class="small muted">Median forecast return ${percent(f.expected_return)} · indicative price ${money(f.last_close*(1+f.expected_return))} on latest completed close ${money(f.last_close)}</span></div><p>${escape(f.reason)}</p>` : `<div>${signal('WAITING')}<h3>${escape(selected)} is waiting for its first trained model.</h3></div><p>Training starts when Alpaca credentials and sufficient history are available.</p>`;
  $('verdict').insertAdjacentHTML('beforeend', `<div class="holding-context small muted">${escape(positionSummary(item.position))}</div>`);
  $('model-cards').innerHTML=(f?.models || [{name:'Ridge regression'},{name:'Extra Trees'},{name:'Histogram gradient boosting'}]).map((m,i)=>`<article class="model-card"><div class="top"><span class="number">FORECAST / 0${i+1}</span>${outlook(m.signal)}</div><h3>${escape(m.name)}</h3><div class="prediction ${(m.signal||'').toLowerCase()}">${percent(m.expected_return)}</div><span class="small muted">${escape(f?.horizon || "Waiting for a forecast")}</span><div class="drivers">${(m.drivers||[]).map(d=>`<div class="driver"><span>${escape((state.trading_mode==='daytrade'?d.feature.replace(/(\d+)d/g,'$1 bars'):d.feature).replaceAll('_',' '))}</span><span>${percent(d.impact)} impact</span></div>`).join('') || '<p class="small muted">Feature sensitivities appear after training.</p>'}</div><p class="small muted">${m.validation ? `Holdout net ${percent(m.validation.net_return)} · ${m.validation.active_days} active days` : 'No validation results yet'}</p></article>`).join('');
  const opinions=r?.opinions || [], setup=f?.setup;
  if(state.trading_mode==='daytrade') $('model-cards').innerHTML += `<article class="model-card llm"><div><div class="top"><span class="number">VOTE / 04 · DAY-TRADING SETUP</span>${outlook(setup?.signal)}</div><h3>VWAP · EMA 9/20 · opening range · volume</h3><p class="small muted">${setup?.checks ? `${setup.bullish_checks} bullish / ${setup.bearish_checks} bearish checks · RSI ${Math.round(setup.rsi)} · VWAP ${money(setup.vwap)} · ATR ${percent(setup.atr)}` : escape(setup?.note || 'Waiting for session bars')}</p></div><div class="drivers">${Object.entries(setup?.checks || {}).map(([k,v])=>`<div class="driver"><span>${escape(k.replaceAll('_',' '))}</span><span>${v?'yes':'no'}</span></div>`).join('')}</div></article>`;
  else $('model-cards').innerHTML += opinions.length ? opinions.map(o=>`<article class="model-card llm"><div><div class="top"><span class="number">NEWS ANALYST / FREELLM</span>${outlook(o.action)}</div><h3>${escape(o.model)}</h3><p class="small muted">Self-assessed confidence ${Math.round(o.confidence*100)}% · ${time(r.at)}</p><span class="small muted">Source IDs: ${o.source_ids.map(escape).join(', ') || 'none'}</span></div><div class="llm-rationale">${escape(o.rationale)}</div></article>`).join('') : `<article class="model-card llm"><div><span class="number">NEWS ANALYST / FREELLM</span><h3>Waiting for an independent opinion</h3></div><p class="muted">${escape(r?.message || state.research_status.status)}. The LLM is asked once per day for all stocks in one request and counts as the fourth vote; neutral or unavailable news abstains, so all three prediction models must then agree.</p></article>`;
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
const BOT_TEXT={daily:'Drei Prognose-Modelle plus News. Kauft morgens, hält über Nacht.',daytrade:'Regelbasierte Setups auf Minutenkerzen. Schließt alles vor Börsenschluss.',loop:'Handelt deine grün getesteten Strategien aus dem Trading-Loop.'};
const pct = value => value == null ? '—' : `${value > 0 ? '+' : ''}${Number(value).toFixed(1)} %`;
let allocDirty=false;
function traderCards(s) {
  const bots=(s.strategies||[]).map(st=>`<article class="panel trader-card"><div class="top section-title"><span class="number">${escape(st.name.toUpperCase())}</span><span class="pill">${st.halt?'DRAWDOWN-STOPP':st.entries_enabled?'EINSTIEGE AN':'PAUSIERT'}</span></div><span class="amount">${money(st.capital_ceiling)}</span><span class="small muted">Gewichtung ${st.weight ?? '—'} · ${Math.round(st.capital_fraction*100)} % vom Bot-Budget · investiert höchstens ${money(st.gross_ceiling)}</span><p class="small muted">${escape(BOT_TEXT[st.id]||'')}</p><span class="small muted">${escape(st.engine.status)}${st.id!=='loop'?` / Training: ${escape(st.training.status)}`:''}</span><div class="strategy-actions">${st.id==='loop'?'<a class="button" href="#loop">Strategien</a>':`<button data-view="${escape(st.id)}" aria-pressed="${st.id===selectedStrategy}">${st.id===selectedStrategy?'Prognosen sichtbar':'Prognosen ansehen'}</button>`}<button data-strategy="${escape(st.id)}" data-action="${st.entries_enabled?'pause':'resume'}" ${!s.configured?'disabled':''}>${st.entries_enabled?'Einstiege pausieren':'Einstiege einschalten'}</button></div></article>`);
  const mine=s.manual?.positions||[], value=mine.reduce((sum,p)=>sum+Number(p.market_value||0),0), pl=mine.reduce((sum,p)=>sum+Number(p.unrealized_pl||0),0);
  bots.push(`<article class="panel trader-card"><div class="top section-title"><span class="number">SELBER TRADEN</span><span class="pill">DU</span></div><span class="amount">${money(value)}</span><span class="small muted">${mine.length} eigene Position${mine.length===1?'':'en'} · ${money(pl)} offen</span><p class="small muted">Du kaufst und verkaufst selbst. Die Bots beobachten deine Aktien und geben Hinweise. Außerhalb des Bot-Budgets.</p><div class="strategy-actions"><a class="button" href="#manual">Order aufgeben</a></div></article>`);
  $('strategy-cards').innerHTML=bots.join('');
}
function allocationValues() {
  return {capital_fraction:Number($('alloc-fraction').value)/100, capital_cap:Number($('alloc-cap').value||0),
    weights:{daily:Number($('alloc-daily').value),daytrade:Number($('alloc-daytrade').value),loop:Number($('alloc-loop').value)}};
}
function fillAllocation(a) {
  $('alloc-fraction').value=Math.round(a.capital_fraction*100); $('alloc-cap').value=a.capital_cap||0;
  for(const bot of ['daily','daytrade','loop']) $('alloc-'+bot).value=a.weights[bot];
  allocDirty=false;
}
function allocationPreview() {
  const a=state?.allocation; if(!a) return;
  const v=allocationValues(), equity=Number(state.account?.equity||0), total=Object.values(v.weights).reduce((x,y)=>x+y,0);
  let budget=equity*v.capital_fraction; if(v.capital_cap) budget=Math.min(budget,v.capital_cap);
  for(const bot of ['daily','daytrade','loop']) $('out-'+bot).textContent=`${v.weights[bot]} → ${total?Math.round(v.weights[bot]/total*100):0} %`;
  $('allocation-preview').innerHTML=[['Bot-Budget',money(budget)],...['daily','daytrade','loop'].map(bot=>[{daily:'Prediction-Bot',daytrade:'Daytrading-Bot',loop:'Trading-Loop'}[bot],money(total?budget*v.weights[bot]/total:0)])].map(([k,x])=>`<div><span>${k}</span><strong>${escape(x)}</strong></div>`).join('');
  $('allocation-note').textContent=`Jeder Bot investiert höchstens ${Math.round(a.gross_fraction*100)} % seines Anteils gleichzeitig und höchstens ${Math.round(a.position_fraction*100)} % pro Aktie. Gewichtung 0 heißt: dieser Bot kauft nichts mehr; offene Positionen behalten ihre Schutz-Ausstiege. ${allocDirty?'Noch nicht gespeichert.':''}`;
}
function renderAllocation(s) {
  if(!s.allocation) return;
  if(!allocDirty) fillAllocation(s.allocation);
  $('allocation-saved').textContent=s.allocation.saved_at?`Gespeichert ${time(s.allocation.saved_at)}`:'Standardwerte aus .env';
  allocationPreview();
}
const LOOP_STATUS={gruen:['GRÜN','buy'],rot:['ROT','sell'],geaendert:['GEÄNDERT','hold'],veraltet:['NEU TESTEN','hold'],ungetestet:['UNGETESTET','hold'],nicht_handelbar:['NICHT HANDELBAR','hold']};
function renderLoop(s) {
  const l=s.loop; $('loop').hidden=!l; if(!l) return;
  const st=(s.strategies||[]).find(x=>x.id==='loop');
  $('loop-state').textContent=l.halt?'DRAWDOWN-STOPP':st?.entries_enabled?'EINSTIEGE AN':'PAUSIERT';
  $('loop-halt').hidden=!l.halt;
  if(l.halt) $('loop-halt-text').textContent=`Maximaler Drawdown erreicht (${l.halt.drawdown} % ≥ ${l.halt.limit} %, ${time(l.halt.at)}). Der Trading-Loop hat seine Positionen geschlossen und kauft nichts mehr. Zurücksetzen startet die Messung neu; Einstiege schaltest du danach selbst wieder ein.`;
  $('loop-summary').innerHTML=[['Risikoregeln',l.rules.ok?`Stop ${l.rules.stop_loss} % · Drawdown ${l.rules.max_drawdown} %`:l.rules.message],['Drawdown jetzt',l.drawdown==null?'—':`${l.drawdown.toFixed(1)} %`],['Eingeschaltet',`${l.running.length} Version${l.running.length===1?'':'en'}`],['Ordner',l.available?'verbunden':'nicht gefunden']].map(([k,v])=>`<div><span>${k}</span><strong class="${k==='Risikoregeln'&&!l.rules.ok?'sell':''}">${escape(v)}</strong></div>`).join('');
  const testCell=t=>t?`<span class="pill ${t.gruen?'buy':'sell'}">${t.gruen?'GRÜN':'ROT'}</span><br><small class="muted">${pct(t.rendite)} · DD ${pct(t.max_drawdown)} · ${escape(t.trades)} Trades</small>`:'—';
  $('loop-running').innerHTML=l.running.map(v=>`<div class="loop-version"><div class="section-title"><div><b>${escape(v.version)}</b> <span class="small muted">${v.symbol==='LISTE'?'Aktienliste der Bots + deine Aktien':escape(v.symbol)} · ${v.green} von ${v.rows.length} Aktien im Test grün · ${v.computed_at?'berechnet '+time(v.computed_at):'wird berechnet…'}</span></div><button data-loop="deactivate" data-version="${escape(v.version)}">Abschalten</button></div>${v.error?`<p class="sell small">${escape(v.error.message)}</p>`:''}<div class="table-wrap"><table><thead><tr><th>Aktie</th><th>Test (Bot)</th><th>Signal</th><th>Entscheidung</th><th>Position</th></tr></thead><tbody>${v.rows.map(r=>`<tr><td>${escape(r.symbol)}${r.own?'<br><small class="muted">deine Aktie</small>':''}</td><td>${r.result?.error?`<small class="sell">${escape(r.result.error)}</small>`:testCell(r.result?.test)}</td><td>${r.result?.signal?`${signal(r.result.signal)}<br><small class="muted">Kerze ${escape(r.result.to)}</small>`:'—'}</td><td>${r.action?signal(r.action):'—'}<br><small class="muted">${escape(r.reason||'')}</small></td><td>${r.position?.qty?`${escape(r.position.qty)} Stück · ${money(Number(r.position.unrealized_pl))}`:'—'}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">Die Aktienliste ist noch leer. Sie füllt sich, sobald der Scanner gelaufen ist.</td></tr>'}</tbody></table></div></div>`).join('')||'<p class="empty">Noch keine Strategie eingeschaltet. Schalte unten eine grüne Version ein und gib dem Trading-Loop eine Gewichtung über 0.</p>';
  $('loop-versions').innerHTML=l.versions.map(v=>{const r=v.report||{},[label,tone]=LOOP_STATUS[v.status]||[v.status,'hold'];return `<tr><td>${escape(v.version)}</td><td>${v.symbol==='LISTE'?`Liste${r.anzahl?` (${escape(r.gruen_anzahl)}/${escape(r.anzahl)} grün)`:''}`:escape(v.symbol||'—')}</td><td>${pct(r.rendite)}</td><td>${pct(r.kaufen_halten)}</td><td>${pct(r.max_drawdown)}</td><td>${escape(r.trades??'—')}</td><td><span class="pill ${tone}">${label}</span><br><small class="muted">${escape(v.note)}</small></td><td>${v.active?`<button data-loop="deactivate" data-version="${escape(v.version)}">Abschalten</button>`:v.tradable?`<button data-loop="activate" data-version="${escape(v.version)}">Einschalten</button>`:''}</td></tr>`;}).join('')||`<tr><td colspan="8" class="empty">${l.available?'Noch keine Strategien im Ordner strategien/.':'Ordner trading-loop nicht gefunden (TRADING_LOOP_DIR).'}</td></tr>`;
  $('loop-errors').innerHTML=l.errors.map(e=>`<div class="log-row"><time>${escape(e.datum)}</time><span><b>${escape(e.strategie)} v${escape(e.version)}</b>${e.verlust_trades!=null?` · ${escape(e.verlust_trades)} Verlust-Trades`:''} · ${escape(e.problem)} → ${escape(e.aenderung)}${e.ergebnis?` (danach ${escape(e.ergebnis)})`:''}</span></div>`).join('')||'<p class="empty">Noch keine Fehler gespeichert.</p>';
}
function renderManual(s) {
  const m=s.manual; $('manual').hidden=!m; if(!m) return;
  $('manual-mode').textContent=s.mode.toUpperCase();
  $('manual-orders').innerHTML=m.orders.map(o=>`<div class="log-row"><time>${time(o.submitted_at)}</time><span>${signal(o.side.toUpperCase())} ${escape(o.qty??('$'+o.notional))} ${escape(o.symbol)} · ${escape(o.type)}${o.limit_price?` ${money(Number(o.limit_price))}`:''}${o.stop_price?` Stop ${money(Number(o.stop_price))}`:''} · ${escape(o.status)}</span><span class="row-actions"><button data-cancel="${escape(o.id)}">Stornieren</button></span></div>`).join('')||'<p class="empty">Keine offenen Orders.</p>';
  $('manual-history').innerHTML=m.history.map(h=>`<div class="log-row"><time>${time(h.at)}</time><span>${signal(h.side.toUpperCase())} ${escape(h.qty)} ${escape(h.symbol)} · ${escape(h.status)}</span></div>`).join('')||'<p class="empty">Noch keine eigenen Orders.</p>';
  $('manual-positions').innerHTML=m.positions.map(p=>`<div class="holding"><div><b>${escape(p.symbol)}</b><div class="small muted">${escape(p.qty)} Stück · ${money(Number(p.market_value))} · ${p.owner==='manual'?'über das Dashboard gekauft':'direkt bei Alpaca gekauft'}</div><div class="small ${Number(p.unrealized_pl)<0?'sell':'buy'}">${money(Number(p.unrealized_pl))} (${percent(Number(p.unrealized_plpc))}) offen</div><div class="small muted">${p.outlook.protected?'Stop-Loss beim Broker aktiv':'Kein Stop-Loss beim Broker'}</div><div class="row-actions spaced"><button data-sell="${escape(p.symbol)}" data-qty="${escape(p.qty)}">Verkaufen…</button></div></div><div><div class="views">${p.outlook.views.map(v=>`<div><span>${escape(v.name)}:</span> ${escape(v.text)}</div>`).join('')}</div><p class="hint ${p.outlook.tone}">${escape(p.outlook.hint)}</p></div></div>`).join('')||'<p class="empty">Du hältst gerade keine eigenen Aktien.</p>';
}
async function api(path, body) {
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Bot-Control':'dashboard'},body:JSON.stringify(body)});
  const result=await r.json().catch(()=>({error:`Anfrage fehlgeschlagen (${r.status})`}));
  if(!r.ok) throw Error(result.error||'Aktion fehlgeschlagen');
  return result;
}
function render(s) {
  state=s; selectedStrategy=s.trading_mode; selected ||= s.symbols[0]?.symbol;
  const parallel=s.portfolio_mode==='both', strategies=s.strategies||[];
  $('bots').hidden=!parallel;
  if(parallel) { traderCards(s); renderAllocation(s); }
  renderLoop(s); renderManual(s);
  const intraday=s.trading_mode==='daytrade', universe=s.universe||{};
  $('strategy-label').textContent=intraday?'DAY TRADING':'DAILY';
  $('strategy-summary').textContent=intraday?`Five-minute bars / 15-minute forecasts. Risk polling every ${s.settings.poll_seconds}s; up to ${s.settings.daytrade_max_positions} positions. Timed exits after ${s.settings.daytrade_hold_minutes} minutes; flattening starts ${s.settings.daytrade_close_minutes} minutes before market close. Paper only.`:`Daily forecasts. New regular-session entries: 09:35-09:59 New York time. ${s.settings.queue_daily_orders?'When closed, limit orders may queue for the next regular session and can fill from its open.':'Closed-market queuing is disabled.'}`;
  $('scan-summary').textContent=s.settings.universe_mode==='market'?`${s.scan_status.status} / ${universe.total_assets||0} tradable listed assets / ${universe.covered||0} with snapshots / ${universe.qualified||0} passed filters / ${(universe.symbols||[]).length} shortlisted / ${time(universe.at)}${s.scan_status.message?' / '+s.scan_status.message:''}`:`Fixed watchlist / ${s.symbols.length} symbols`;
  $('scan-candidates').innerHTML=(universe.candidates||[]).map(c=>{const f=s.symbols.find(x=>x.symbol===c.symbol)?.forecast;return `<tr><td>${escape(c.symbol)}</td><td>${money(c.price)}</td><td>${percent(c.change)}</td><td>${money(c.dollar_volume)}</td><td>${percent(f?.expected_return)}</td><td>${signal(f?.action||'UNTRAINED')}</td></tr>`;}).join('') || '<tr><td colspan="6" class="empty">No market candidates available yet.</td></tr>';
  $('connection').textContent=(parallel?s.portfolio_engine?.status||s.engine.status:s.engine.status).toUpperCase();
  $('side-mode').textContent=`${s.mode.toUpperCase()} TRADING`; $('mode-label').textContent=s.mode.toUpperCase();
  $('equity').textContent=money(s.account?.equity); $('pnl').textContent=money(s.account?.daily_pnl);
  $('pnl').className=(s.account?.daily_pnl||0)<0?'sell':'buy';
  $('capital').textContent=money(s.settings.capital); $('allocation').textContent=`${Math.round(s.settings.capital_fraction*100)}% of equity${s.settings.capital_cap?` (cap ${money(s.settings.capital_cap)})`:''} · ${Math.round(s.settings.position_fraction*100)}% per position · ${Math.round(s.settings.gross_fraction*100)}% max gross`;
  $('entry-state').textContent=parallel?`${strategies.filter(x=>x.entries_enabled).length} / ${strategies.length} enabled`:s.entries_enabled?'Enabled':'Paused';
  $('toggle').textContent=parallel?(s.any_entries_enabled?'Alle Bots pausieren':'Alle Bots einschalten'):(s.entries_enabled?'Pause entries':`Enable ${s.mode} entries`); $('toggle').disabled=!s.configured;
  $('train').disabled=!s.configured||['running','starting'].includes(s.training.status);
  $('notice').textContent=!s.configured?'Setup needed: add Alpaca keys and a dashboard password to .env, then restart.':s.account?.loss_halt?'Daily loss limit reached. New entries are disabled; protective exits remain active.':`${(parallel?s.portfolio_engine?.message:null) || s.engine.message || s.engine.status} · ${s.settings.feed.toUpperCase()} data · ${s.account?.market_open?'Market open':'Market closed'} · ${s.settings.web_search?'Alpaca + Brave news search':'Alpaca news; optional Brave search not configured'}`;
  $('memory').textContent=`${s.resources.process_memory_mb.toFixed(0)} MB / 6 GB`; $('memory-bar').value=s.resources.process_memory_mb;
  $('train-memory').textContent=`${(s.resources.training_memory_mb/1024).toFixed(1)} GB`;
  $('training-status').textContent=`${s.training.status}${s.training.symbol?' · '+s.training.symbol:''}${s.training.model?' / '+s.training.model:''}`;
  $('training-log').innerHTML=s.training_history.slice(0,12).map(e=>`<div class="log-row"><time>${time(e.at)}</time><span>${escape(e.symbol||'')} ${escape(e.model||'')} · ${escape(e.message)}</span></div>`).join('');
  $('orders').innerHTML=s.orders.map(o=>`<tr><td>${time(o.at)}</td><td>${escape(o.symbol)}</td><td>${escape(o.strategy||'daily')}</td><td>${signal(o.side.toUpperCase())}</td><td>${escape(o.qty)}</td><td>${escape(o.filled_qty??'—')}</td><td>${o.filled_avg_price?money(Number(o.filled_avg_price)):'—'}</td><td>${escape(o.status)}${o.queued_for_open?`<br><small>Queued for ${escape(o.session)} / limit ${money(Number(o.limit_price))}</small>`:o.limit_price?`<br><small>Limit ${money(Number(o.limit_price))}</small>`:''}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">No orders submitted.</td></tr>';
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
$('allocation-form').addEventListener('input',()=>{allocDirty=true;allocationPreview();});
$('alloc-reset').addEventListener('click',()=>{if(state?.allocation){fillAllocation(state.allocation);allocationPreview();}});
$('allocation-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/allocation',allocationValues());allocDirty=false;toast('Budget und Gewichtung gespeichert. Gilt ab der nächsten Order.');await refresh();}catch(err){toast(err.message);}});
async function loopAction(action,version){try{const r=await api('/api/loop',{action,version});toast(r.message||'Gespeichert.');await refresh();}catch(err){toast(err.message);}}
for(const id of ['loop-running','loop-versions']) $(id).addEventListener('click',e=>{const b=e.target.closest('[data-loop]');if(!b)return;if(b.dataset.loop==='activate'&&!confirm(`${b.dataset.version} im Paper-Modus handeln lassen? Der Trading-Loop braucht dafür eine Gewichtung über 0 und eingeschaltete Einstiege.`))return;loopAction(b.dataset.loop,b.dataset.version);});
$('loop-reset').addEventListener('click',()=>{if(confirm('Höchststand zurücksetzen? Die Drawdown-Messung beginnt dann beim aktuellen Kontowert neu.'))loopAction('reset_peak');});
$('m-type').addEventListener('change',()=>{$('m-limit').disabled=$('m-type').value!=='limit';});
$('manual-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const side=$('m-side').value, unit=$('m-unit').value, amount=Number($('m-amount').value), type=$('m-type').value;
  const body={symbol:$('m-symbol').value.trim().toUpperCase(),side,type,[unit]:amount,limit_price:type==='limit'?Number($('m-limit').value):null,
    stop_loss_pct:$('m-stop').value?Number($('m-stop').value):null,take_profit_pct:$('m-target').value?Number($('m-target').value):null};
  const what=`${side==='buy'?'Kaufen':'Verkaufen'}: ${unit==='qty'?amount+' Stück':money(amount)} ${body.symbol} als ${type==='market'?'Market-Order':'Limit-Order zu '+money(body.limit_price)}${body.stop_loss_pct?`, Stop-Loss ${body.stop_loss_pct} %`:''}${body.take_profit_pct?`, Kursziel ${body.take_profit_pct} %`:''}`;
  if(!body.symbol||!(amount>0)){toast('Symbol und Menge angeben.');return;}
  if(!confirm(`${what}\n\nOrder an Alpaca (${state?.mode==='live'?'ECHTES GELD':'Paper-Konto'}) senden?`))return;
  try{const r=await api('/api/manual/order',body);toast(r.message);await refresh();}catch(err){toast(err.message);}
});
$('manual-orders').addEventListener('click',async e=>{const b=e.target.closest('[data-cancel]');if(!b||!confirm('Diese Order stornieren?'))return;try{const r=await api('/api/manual/cancel',{order_id:b.dataset.cancel});toast(r.message);await refresh();}catch(err){toast(err.message);}});
$('manual-positions').addEventListener('click',e=>{const b=e.target.closest('[data-sell]');if(!b)return;$('m-symbol').value=b.dataset.sell;$('m-side').value='sell';$('m-unit').value='qty';$('m-amount').value=b.dataset.qty;$('m-stop').value='';$('m-target').value='';$('manual-form').scrollIntoView({behavior:'smooth',block:'center'});toast('Formular ausgefüllt. Prüfen und auf „Order prüfen & senden“ klicken.');});
refresh(); setInterval(refresh,5000);
