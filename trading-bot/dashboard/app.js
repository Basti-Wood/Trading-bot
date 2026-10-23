'use strict';
const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = (value, currency='USD') => value == null ? '—' : new Intl.NumberFormat('en-US',{style:'currency',currency,maximumFractionDigits:2}).format(value);
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
  const opinions=r?.opinions || [], setup=f?.playbook;
  if(state.trading_mode==='daytrade') $('model-cards').innerHTML += `<article class="model-card llm"><div class="top"><span class="number">DAY-TRADING PLAYBOOK</span>${outlook(setup?.signal)}</div><h3>One-minute setups</h3><p class="small muted">${escape(setup?.note || setup?.baseline || 'Waiting for session bars')}</p><div class="drivers">${(setup?.signals||[]).map(x=>`<div><b>${escape(x.name)}</b><p>${escape(x.reason)}</p></div>`).join('') || '<p>No setup firing.</p>'}</div></article>`;
  else $('model-cards').innerHTML += opinions.length ? opinions.map(o=>`<article class="model-card llm"><div><div class="top"><span class="number">NEWS ANALYST / FREELLM</span>${outlook(o.action)}</div><h3>${escape(o.model)}</h3><p class="small muted">Self-assessed confidence ${Math.round(o.confidence*100)}% · ${time(r.at)}</p><span class="small muted">Source IDs: ${o.source_ids.map(escape).join(', ') || 'none'}</span></div><div class="llm-rationale">${escape(o.rationale)}</div></article>`).join('') : `<article class="model-card llm"><div><span class="number">NEWS ANALYST / FREELLM</span><h3>Waiting for an independent opinion</h3></div><p class="muted">${escape(r?.message || state.research_status.status)}. Llama is asked separately for each stock each day and counts as the fourth vote; neutral or unavailable news abstains, so all three prediction models must then agree.</p></article>`;
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
const BOT_TEXT={daily:'Drei Prognose-Modelle plus eine News-Stimme pro Aktie.',daytrade:'Regelbasierte Setups auf Minutenkerzen. Schließt vor Börsenschluss.',loop:'Tageskerzen: wählt pro Aktie die beste grüne aktive Version nach Rendite, dann Drawdown.',loop_day:'Intraday-Kerzen: Signal nach jeder Kerze, stündlich neu angepasst, vor Börsenschluss flat.'};
const BOT_NAMES={daily:'Prediction-Bot',daytrade:'Daytrading-Bot',loop:'Trading-Loop',loop_day:'Intraday-Loop'};
const allocBots=()=>(state?.strategies||[]).map(st=>st.id).filter(id=>id in BOT_NAMES);
const pct = value => value == null ? '—' : `${value > 0 ? '+' : ''}${Number(value).toFixed(1)} %`;
let allocDirty=false;
function traderCards(s) {
  const bots=(s.strategies||[]).map(st=>`<article class="panel trader-card"><div class="top section-title"><span class="number">${escape(st.name.toUpperCase())}</span><span class="pill">${st.halt?'DRAWDOWN-STOPP':st.entries_enabled?'EINSTIEGE AN':'PAUSIERT'}</span></div><span class="amount">${money(st.capital_ceiling)}</span><span class="small muted">Gewichtung ${st.weight ?? '—'} · ${Math.round(st.capital_fraction*100)} % vom Bot-Budget · investiert höchstens ${money(st.gross_ceiling)}</span><p class="small muted">${escape(BOT_TEXT[st.id]||'')}</p><span class="small muted">${escape(st.engine.status)}${!st.id.startsWith('loop')?` / Training: ${escape(st.training.status)}`:''}</span><div class="strategy-actions">${st.id.startsWith('loop')?'<a class="button" href="#loop">Strategien</a>':`<button data-view="${escape(st.id)}" aria-pressed="${st.id===selectedStrategy}">${st.id===selectedStrategy?'Prognosen sichtbar':'Prognosen ansehen'}</button>`}<button data-strategy="${escape(st.id)}" data-action="${st.entries_enabled?'pause':'resume'}" ${!s.configured?'disabled':''}>${st.entries_enabled?'Einstiege pausieren':'Einstiege einschalten'}</button></div></article>`);
  $('strategy-cards').innerHTML=bots.join('');
}
function allocationValues() {
  return {capital_fraction:Number($('alloc-fraction').value)/100, capital_cap:Number($('alloc-cap').value||0),
    weights:Object.fromEntries(allocBots().map(bot=>[bot,Number($('alloc-'+bot).value)]))};
}
function fillAllocation(a) {
  $('alloc-fraction').value=Math.round(a.capital_fraction*100); $('alloc-cap').value=a.capital_cap||0;
  for(const label of document.querySelectorAll('#allocation-form [data-bot]')) label.hidden=!allocBots().includes(label.dataset.bot);
  for(const bot of allocBots()) $('alloc-'+bot).value=a.weights[bot]??0;
  allocDirty=false;
}
function allocationPreview() {
  const a=state?.allocation; if(!a) return;
  const v=allocationValues(), equity=Number(state.account?.equity||0), total=Object.values(v.weights).reduce((x,y)=>x+y,0);
  let budget=equity*v.capital_fraction; if(v.capital_cap) budget=Math.min(budget,v.capital_cap);
  for(const bot of allocBots()) $('out-'+bot).textContent=`${v.weights[bot]} → ${total?Math.round(v.weights[bot]/total*100):0} %`;
  $('allocation-preview').innerHTML=[['Bot-Budget',money(budget)],...allocBots().map(bot=>[BOT_NAMES[bot],money(total?budget*v.weights[bot]/total:0)])].map(([k,x])=>`<div><span>${k}</span><strong>${escape(x)}</strong></div>`).join('');
  $('allocation-note').textContent=`Jeder Bot investiert höchstens ${Math.round(a.gross_fraction*100)} % seines Anteils gleichzeitig und höchstens ${Math.round(a.position_fraction*100)} % pro Aktie. Gewichtung 0 heißt: dieser Bot kauft nichts mehr; offene Positionen behalten ihre Schutz-Ausstiege. ${allocDirty?'Noch nicht gespeichert.':''}`;
}
function renderAllocation(s) {
  if(!s.allocation) return;
  if(!allocDirty) fillAllocation(s.allocation);
  $('allocation-saved').textContent=s.allocation.saved_at?`Gespeichert ${time(s.allocation.saved_at)}`:'Standardwerte aus .env';
  allocationPreview();
}
const LOOP_STATUS={bereit:['BEREIT','buy'],gruen:['GRÜN','buy'],rot:['ROT','sell'],geaendert:['GEÄNDERT','hold'],veraltet:['NEU TESTEN','hold'],ungetestet:['UNGETESTET','hold'],nicht_handelbar:['NICHT HANDELBAR','hold']};
function renderLoop(s) {
  const l=s.loop; $('loop').hidden=!l; if(!l) return;
  const st=(s.strategies||[]).find(x=>x.id==='loop');
  $('loop-state').textContent=l.halt?'DRAWDOWN-STOPP':Object.values(l.bots||{}).map(b=>`${b.name}: ${b.entries_enabled?'AN':'PAUSIERT'}`).join(' · ')||(st?.entries_enabled?'EINSTIEGE AN':'PAUSIERT');
  $('loop-halt').hidden=!l.halt;
  if(l.halt) $('loop-halt-text').textContent=`Maximaler Drawdown erreicht (${l.halt.drawdown} % ≥ ${l.halt.limit} %, ${time(l.halt.at)}). Der Trading-Loop hat seine Positionen geschlossen und kauft nichts mehr. Zurücksetzen startet die Messung neu; Einstiege schaltest du danach selbst wieder ein.`;
  $('loop-summary').innerHTML=[['Risikoregeln',l.rules.ok?`Stop ${l.rules.stop_loss} % · Drawdown ${l.rules.max_drawdown} %`:l.rules.message],['Drawdown jetzt',l.drawdown==null?'—':`${l.drawdown.toFixed(1)} %`],['Eingeschaltet',`${l.running.length} Version${l.running.length===1?'':'en'}`],['Ordner',l.available?'verbunden':'nicht gefunden']].map(([k,v])=>`<div><span>${k}</span><strong class="${k==='Risikoregeln'&&!l.rules.ok?'sell':''}">${escape(v)}</strong></div>`).join('');
  const rt=l.retest, testing=l.retest_requested||l.retest_running, done=Object.entries(rt?.done||{});
  $('loop-retest').disabled=testing||!l.available;
  $('loop-retest').textContent=testing?'Test läuft…':'Alle neu testen';
  const failed=done.filter(([,r])=>r.startsWith('Fehler'));
  const rd=l.bots?.loop_day?.retest, intradayText=rd?` Intraday-Loop: ${rd.status==='running'?`läuft (${rd.trigger}) ${rd.current||''}`:`zuletzt ${rd.trigger} ${time(rd.at||rd.started_at)}, ${rd.status}`}.`:'';
  $('loop-retest-status').textContent=`Tageskerzen-Versionen werden jeden Tag ab ${l.retest_hour}:00 New Yorker Zeit neu getestet und angepasst, Intraday-Versionen zusätzlich alle ${l.intraday_retest_minutes} Minuten während der Handelszeit. Die Strategie-Dateien ändern sich dabei nicht.${intradayText} `+
    (l.retest_requested?'Stresstest startet gleich…':rt?.status==='running'?`Stresstest läuft (${rt.trigger}): ${rt.current||''} · ${done.length} von ${rt.versions.length} fertig.`:
     rt?`Letzter Lauf (${rt.trigger}) ${time(rt.at||rt.started_at)}: ${rt.status==='fertig'?`${done.length} Version${done.length===1?'':'en'} getestet`:rt.status}${rt.message?' · '+rt.message:''}${failed.length?' · '+failed.map(([v,r])=>`${v}: ${r}`).join(' · '):''}`:'');
  const testCell=t=>t?`<span class="pill ${t.gruen?'buy':'sell'}">${t.gruen?'GRÜN':'ROT'}</span><br><small class="muted">${pct(t.rendite)} · DD ${pct(t.max_drawdown)} · ${escape(t.trades)} Trades</small>`:'—';
  $('loop-running').innerHTML=`<div class="table-wrap"><table><thead><tr><th>Broker / Aktie</th><th>Beste grüne Version / Besitzer</th><th>Einzeltest</th><th>Strategie-Signal</th><th>Entscheidung</th></tr></thead><tbody>${(l.stocks||[]).map(r=>`<tr><td><small>${r.broker==='trading212'?'Trading 212':'Alpaca'}</small><br>${escape(r.symbol)}</td><td>${escape(r.version||'—')}${r.owner?`<br><small>Besitzer: ${escape(r.owner)}</small>`:''}</td><td>${testCell(r.test)}</td><td>${signal(r.signal||'HOLD')}</td><td>${signal(r.action||'WAIT')}<br><small class="muted">${escape(r.reason)}</small><details><summary>Alle Versionen</summary>${Object.entries(r.views||{}).map(([v,i])=>`<div>${escape(v)}: ${escape(i.signal||i.error||'—')} · ${pct(i.test?.rendite)} · DD ${pct(i.test?.max_drawdown)} · ${i.test?.gruen?'GRÜN':'ROT'}</div>`).join('')}</details></td></tr>`).join('')||'<tr><td colspan="5" class="empty">Aktiviere getestete Versionen und gib dem Trading-Loop eine Gewichtung über 0.</td></tr>'}</tbody></table></div>` + l.running.map(v=>`<div class="loop-version section-title"><span><b>${escape(v.version)}</b> · ${v.green}/${v.rows.length} Einzeltests grün · ${v.computed_at?time(v.computed_at):'wird berechnet…'}${v.error?` · ${escape(v.error.message)}`:''}</span><button data-loop="deactivate" data-version="${escape(v.version)}">Abschalten</button></div>`).join('');
  $('loop-versions').innerHTML=l.versions.map(v=>{const r=v.report||{},[label,tone]=LOOP_STATUS[v.status]||[v.status,'hold'];return `<tr><td>${escape(v.version)}</td><td>${v.symbol==='LISTE'?`Liste (${escape(r.gruen_anzahl??'—')}/${escape(r.anzahl??'—')} grün)`:escape(v.symbol||'—')}</td><td>${pct(r.rendite)}</td><td>${pct(r.kaufen_halten)}</td><td>${pct(r.max_drawdown)}</td><td>${escape(r.trades??'—')}</td><td><span class="pill ${tone}">${escape(label)}</span><br><small class="muted">${escape(v.note)}</small></td><td><div class="row-actions">${v.active?`<button data-loop="deactivate" data-version="${escape(v.version)}">Abschalten</button>`:v.tradable?`<button data-loop="activate" data-version="${escape(v.version)}">Einschalten</button>`:''}<button data-loop="retest" data-version="${escape(v.version)}" ${testing?'disabled':''}>Neu testen</button></div></td></tr>`;}).join('')||'<tr><td colspan="8" class="empty">Keine Strategie-Versionen gefunden.</td></tr>';
  $('loop-errors').innerHTML=l.errors.map(e=>`<div class="log-row"><time>${escape(e.datum)}</time><span><b>${escape(e.strategie)} v${escape(e.version)}</b>${e.verlust_trades!=null?` · ${escape(e.verlust_trades)} Verlust-Trades`:''} · ${escape(e.problem)} → ${escape(e.aenderung)}${e.ergebnis?` (danach ${escape(e.ergebnis)})`:''}</span></div>`).join('')||'<p class="empty">Noch keine Fehler gespeichert.</p>';
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
  renderLoop(s); renderNewsReport(s);
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
  $('notice').textContent=!s.configured?'Setup needed: add Alpaca keys and a dashboard password to .env, then restart.':s.account?.loss_halt?'Daily loss limit reached. New entries are disabled; protective exits remain active.':`${(parallel?s.portfolio_engine?.message:null) || s.engine.message || s.engine.status} · ${s.settings.feed.toUpperCase()} data · ${s.account?.market_open?'Market open':'Market closed'} · Alpaca news · per-stock Llama reports`;
  $('memory').textContent=s.resources.available===false?'Telemetry unavailable':`${s.resources.process_memory_mb.toFixed(0)} MB / 6 GB`; $('memory-bar').value=s.resources.process_memory_mb;
  $('train-memory').textContent=`${(s.resources.training_memory_mb/1024).toFixed(1)} GB`;
  $('training-status').textContent=`${s.training.status}${s.training.symbol?' · '+s.training.symbol:''}${s.training.model?' / '+s.training.model:''}`;
  $('training-log').innerHTML=s.training_history.slice(0,12).map(e=>`<div class="log-row"><time>${time(e.at)}</time><span>${escape(e.symbol||'')} ${escape(e.model||'')} · ${escape(e.message)}</span></div>`).join('');
  $('orders').innerHTML=s.orders.map(o=>`<tr><td>${time(o.at)}</td><td><small>${o.broker==='trading212'?'Trading 212':'Alpaca'}</small><br>${escape(o.symbol)}</td><td>${escape(o.strategy||'daily')}</td><td>${signal(o.side.toUpperCase())}</td><td>${escape(o.qty)}</td><td>${escape(o.filled_qty??'—')}</td><td>${o.filled_avg_price?money(Number(o.filled_avg_price),o.currency||'USD'):'—'}</td><td>${escape(o.status)}${o.queued_for_open?`<br><small>Queued for ${escape(o.session)} / limit ${money(Number(o.limit_price),o.currency||'USD')}</small>`:o.limit_price?`<br><small>Limit ${money(Number(o.limit_price),o.currency||'USD')}</small>`:''}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">No orders submitted.</td></tr>';
  $('positions').innerHTML=(s.account?.positions||[]).map(p=>`<div class="position"><b>${escape(p.symbol)}${p.strategy?` / ${escape(p.strategy)}`:''}</b><span>${escape(p.qty)} shares</span><span>${money(Number(p.market_value))}</span><span>${money(Number(p.unrealized_pl))} unrealized</span></div>`).join('') || 'No open positions.';
  $('updated').textContent=`LAST UPDATED ${time(s.at)}`;
  renderMoney(s); renderTrading212(s); drawChart(s.equity); renderModels(); renderCandles();
}
async function refresh() {const serial=++refreshSerial;try{const r=await fetch('/api/state'+(selectedStrategy?'?strategy='+encodeURIComponent(selectedStrategy):''));if(!r.ok)throw Error(`Dashboard request failed (${r.status})`);const snapshot=await r.json();if(serial===refreshSerial)render(snapshot);}catch(e){$('connection').textContent='OFFLINE';$('notice').textContent=e.message+' · displayed information may be stale.';}}
async function control(action,strategy='all') {try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-Bot-Control':'dashboard'},body:JSON.stringify({action,strategy})});const body=await r.json();if(!r.ok)throw Error(body.error||'Action failed');toast(action==='train'?'Retraining queued.':'Entry setting saved.');await refresh();}catch(e){toast(e.message);}}
$('toggle').addEventListener('click',()=>control((state.portfolio_mode==='both'?state.any_entries_enabled:state.entries_enabled)?'pause':'resume'));
$('train').addEventListener('click',()=>control('train',selectedStrategy||'all'));
$('symbols').addEventListener('click',e=>{const b=e.target.closest('[data-symbol]');if(b){selected=b.dataset.symbol;renderModels();renderCandles();renderForecast();}});
$('strategy-cards').addEventListener('click',e=>{const view=e.target.closest('[data-view]'), toggle=e.target.closest('[data-strategy]');if(view){selectedStrategy=view.dataset.view;selected=null;refresh();}else if(toggle){control(toggle.dataset.action,toggle.dataset.strategy);}});
$('allocation-form').addEventListener('input',()=>{allocDirty=true;allocationPreview();});
$('alloc-reset').addEventListener('click',()=>{if(state?.allocation){fillAllocation(state.allocation);allocationPreview();}});
$('allocation-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/allocation',allocationValues());allocDirty=false;toast('Budget und Gewichtung gespeichert. Gilt ab der nächsten Order.');await refresh();}catch(err){toast(err.message);}});
async function loopAction(action,version){try{const r=await api('/api/loop',{action,version});toast(r.message||'Gespeichert.');await refresh();}catch(err){toast(err.message);}}
for(const id of ['loop-running','loop-versions']) $(id).addEventListener('click',e=>{const b=e.target.closest('[data-loop]');if(!b)return;if(b.dataset.loop==='activate'&&!confirm(`${b.dataset.version} im Paper-Modus handeln lassen? Der Trading-Loop braucht dafür eine Gewichtung über 0 und eingeschaltete Einstiege.`))return;loopAction(b.dataset.loop,b.dataset.version);});
$('loop-retest').addEventListener('click',()=>loopAction('retest'));
$('loop-reset').addEventListener('click',()=>{if(confirm('Höchststand zurücksetzen? Die Drawdown-Messung beginnt dann beim aktuellen Kontowert neu.'))loopAction('reset_peak');});
function renderNewsReport(s) {
  const r=s.news_report;
  $('news-report-date').textContent=r?`${r.day} · updated ${time(r.at)}`:'Waiting for daily research';
  $('market-overview').textContent=r?.overview?.summary || r?.overview?.message || 'The market overview will appear after daily news research.';
  const sources=r?.overview?.sources||[];
  $('market-sources').innerHTML=sources.filter(n=>r.overview.source_ids?.includes(n.id)).map(n=>{let u;try{u=new URL(n.url);}catch{return '';}return ['http:','https:'].includes(u.protocol)?`<a href="${escape(u.href)}" target="_blank" rel="noopener noreferrer">${escape(n.title)}</a>`:'';}).join(' · ');
  $('stock-news-report').innerHTML=Object.values(r?.stocks||{}).sort((a,b)=>a.symbol.localeCompare(b.symbol)).map(x=>`<tr><td>${escape(x.symbol)}</td><td>${signal(x.action||'HOLD')}</td><td>${escape(x.summary||x.message||'No summary available')}<br><small class="muted">${escape(x.opinions?.[0]?.model||'')} · ${time(x.at)}</small></td></tr>`).join('')||'<tr><td colspan="3" class="empty">No stock summaries yet.</td></tr>';
}
let candleSerial=0;
async function renderCandles() {
  const serial=++candleSerial, symbol=selected;
  $('day-chart').hidden=state?.trading_mode!=='daytrade';
  if($('day-chart').hidden) return;
  $('candle-title').textContent=`${symbol||'Stock'} · today’s one-minute bars`;
  if(!symbol) {$('candles').textContent='Choose a monitored stock.';return;}
  try {
    const response=await fetch('/api/candles/'+encodeURIComponent(symbol));
    const data=await response.json();
    if(serial!==candleSerial) return;
    if(!response.ok) throw Error(data.error||'Candle data unavailable');
    drawCandles(data);
  } catch(err) {if(serial===candleSerial){$('candles').textContent=err.message;$('candle-status').textContent='UNAVAILABLE';}}
}
function drawCandles(data) {
  const bars=data.bars||[], forecast=data.forecast;
  const stale=Date.now()-Date.parse(data.at)>90000;
  $('candle-status').textContent=stale?'CACHED':'COMPLETED BARS';
  $('candle-note').textContent=`${data.day} · America/New_York · ${bars.length} completed bars · updated ${time(data.at)}. ${forecast?'Dashed line: actual 15-minute model forecast'+(forecast.eligible?' (validated).':' (research only).'):'No current 15-minute model forecast available.'} Triangles mark observed setup signals; fills are in the execution ledger. ${data.baseline==='ready'?'':'Opening-range/noise baseline unavailable.'}`;
  if(!bars.length) {$('candles').textContent='No completed regular-session bars today.';return;}
  const keys=['vwap','ema9','ema20','opening_high','opening_low','noise_upper','noise_lower'];
  const colors={vwap:'#c4a572',ema9:'#6ca5da',ema20:'#b69ad7',opening_high:'#83b9a3',opening_low:'#83b9a3',noise_upper:'#d39462',noise_lower:'#d39462'};
  const series=data.indicators||{};
  const prices=bars.flatMap(b=>[b.low,b.high]).concat(keys.flatMap(k=>(series[k]||[]).filter(Number.isFinite)));
  if(forecast)prices.push(forecast.target_price,forecast.price);
  const lo=Math.min(...prices),hi=Math.max(...prices),pad=(hi-lo||hi*.002)*.06;
  const first=Date.parse(bars[0].at),last=Date.parse(bars.at(-1).at);
  const end=Math.max(last+60000,forecast?Date.parse(forecast.to):0);
  const x=t=>65+(t-first)/Math.max(60000,end-first)*850;
  const y=p=>310-(p-lo+pad)/(hi-lo+2*pad)*280;
  const width=Math.max(.7,Math.min(10,850/(Math.max(1,(end-first)/60000)+1)*.72));
  let svg='<svg viewBox="0 0 1000 420" role="img" aria-label="One-minute candlesticks with playbook indicators and model forecast">';
  for(let i=0;i<5;i++) {const p=lo+(hi-lo)*i/4,yy=y(p);svg+=`<line class="chart-grid" x1="60" x2="930" y1="${yy}" y2="${yy}"/><text x="940" y="${yy+4}">${escape(money(p))}</text>`;}
  const volume=Math.max(1,...bars.map(b=>b.volume));
  bars.forEach(b=>{const xx=x(Date.parse(b.at)),color=b.close>=b.open?'#70b79e':'#df8d81';svg+=`<g><title>${escape(time(b.at))} O ${b.open.toFixed(2)} H ${b.high.toFixed(2)} L ${b.low.toFixed(2)} C ${b.close.toFixed(2)} · volume ${b.volume}</title><line x1="${xx}" x2="${xx}" y1="${y(b.high)}" y2="${y(b.low)}" stroke="${color}"/><rect x="${xx-width/2}" y="${Math.min(y(b.open),y(b.close))}" width="${width}" height="${Math.max(1,Math.abs(y(b.open)-y(b.close)))}" fill="${color}"/><rect x="${xx-width/2}" y="${375-b.volume/volume*40}" width="${width}" height="${b.volume/volume*40}" fill="${color}" opacity=".4"/></g>`;});
  for(const key of keys) {let path='',connected=false;(series[key]||[]).forEach((v,i)=>{if(!Number.isFinite(v)||!bars[i]){connected=false;return;}path+=`${connected?'L':'M'}${x(Date.parse(bars[i].at))},${y(v)} `;connected=true;});svg+=`<path d="${path}" fill="none" stroke="${colors[key]}" stroke-width="1.5" ${key.startsWith('noise')||key.startsWith('opening')?'stroke-dasharray="4 3"':''}/>`;}
  for(const s of data.signals||[]) {const bar=bars.find(b=>b.at===s.bar_at);if(!bar)continue;const xx=x(Date.parse(s.bar_at)),yy=y(bar.low)+12;svg+=`<path d="M${xx},${yy-5} l-4,8 h8 Z" fill="#b6df9c"><title>${escape(s.name)}: ${escape(s.reason)}</title></path>`;}
  if(forecast)svg+=`<path d="M${x(Date.parse(forecast.from))},${y(forecast.price)} L${x(Date.parse(forecast.to))},${y(forecast.target_price)}" fill="none" stroke="#e5cb86" stroke-width="2" stroke-dasharray="6 4"/><circle cx="${x(Date.parse(forecast.to))}" cy="${y(forecast.target_price)}" r="3" fill="#e5cb86"/>`;
  for(let i=0;i<5;i++) {const t=first+(end-first)*i/4;svg+=`<text x="${x(t)}" y="405" text-anchor="middle">${escape(new Date(t).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false}))}</text>`;}
  $('candles').innerHTML=svg+'</svg>';
}
function renderMoney(s) {
  const m=s.money; if(!m)return;
  $('combined-money').textContent=money(m.combined.equity,m.combined.currency);
  $('combined-percent').textContent=percent(m.combined.return_fraction);
  $('combined-percent').className=(m.combined.return_fraction||0)<0?'sell':'buy';
  $('combined-money-note').textContent=`${m.combined.message}${m.combined.fx_dates?.length?' · FX '+m.combined.fx_dates.join(', '):''}`;
  for(const broker of ['alpaca','trading212']) {
    const b=m[broker];
    $(broker+'-money').textContent=money(b.equity,b.currency||'USD');
    $(broker+'-percent').textContent=percent(b.return_fraction);
    $(broker+'-percent').className=(b.return_fraction||0)<0?'sell':'buy';
    $(broker+'-money-note').textContent=b.status==='disabled'?'Disabled':`${b.mode?.toUpperCase()||''} · ${b.stale?'CACHED / ':''}${b.status} · cash ${money(b.cash,b.currency||'USD')} · ${time(b.at)}`;
    $(broker+'-percent-note').textContent=b.return_error|| (b.since?'Since '+time(b.since):'Waiting for a complete funding-history snapshot');
  }
  $('money-basis').textContent='Money = total account equity (cash + holdings). '+m.return_basis+'. Percentages measure returns, not account allocation. Demo and live balances are never combined.';
}
function renderTrading212(s) {
  const t=s.trading212; $('trading212').hidden=!t?.enabled; if(!t?.enabled)return;
  $('t212-state').textContent=`${t.mode.toUpperCase()} · ${t.engine.status.toUpperCase()}`;
  $('t212-toggle').textContent=t.entries_enabled?'Pause Trading 212 entries':'Enable Trading 212 entries';
  $('t212-toggle').disabled=!t.configured;
  const account=t.account,code=account?.currency||'EUR';
  const budget=Math.min(Number(account?.equity||0)*t.capital_fraction,t.capital_cap||Infinity);
  $('t212-note').textContent=`${t.engine.message||t.engine.status} · Entries ${t.entries_enabled?'enabled':'paused'} · budget ${money(budget,code)} from this account only · ${t.allow_fx?'Foreign-currency orders allowed (FX fees may apply)':'Same-currency entries only'} · ${t.data_configured?'Market-data key configured':'Set TWELVEDATA_API_KEY and instrument mappings'}${t.halt?' · DRAWDOWN STOP ACTIVE':''}`;
  $('t212-positions').innerHTML=(account?.positions||[]).map(p=>`<div class="position"><b>${escape(p.symbol)}</b><span>${escape(p.qty)} shares</span><span>${money(Number(p.market_value),p.currency)}</span><span>${money(Number(p.unrealized_pl),p.currency)} unrealized</span></div>`).join('')||'No open Trading 212 positions.';
}
async function trading212Control(action) {
  try {await api('/api/trading212/control',{action});toast('Trading 212 setting saved.');await refresh();} catch(err){toast(err.message);}
}
$('t212-toggle').addEventListener('click',()=>trading212Control(state?.trading212?.entries_enabled?'pause':'resume'));
$('t212-retest').addEventListener('click',()=>trading212Control('retest'));
$('t212-reset').addEventListener('click',()=>{if(confirm('Reset the Trading 212 drawdown peak? Entries will stay paused.'))trading212Control('reset_peak');});
refresh(); setInterval(refresh,5000);
