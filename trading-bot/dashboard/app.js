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
  const bots=(s.strategies||[]).map(st=>`<article class="panel trader-card"><div class="top section-title"><span class="number">${escape(st.name.toUpperCase())}</span><span class="pill">${st.halt?'DRAWDOWN-STOPP':st.entries_enabled?'EINSTIEGE AN':'PAUSIERT'}</span></div><span class="amount">${money(st.capital_ceiling)}</span><span class="small muted">Gewichtung ${st.weight ?? '—'} · ${Math.round(st.capital_fraction*100)} % vom Bot-Budget · investiert höchstens ${money(st.gross_ceiling)}</span><p class="small muted">${escape(BOT_TEXT[st.id]||'')}</p><span class="small muted ${st.engine.status==='error'?'sell':''}">${escape(st.engine.status==='error'?'Fehler: '+(st.engine.message||''):st.engine.status)}${!st.id.startsWith('loop')?` / Training: ${escape(st.training.status)}${st.training.message?` <span class="muted">(${escape(st.training.message)})</span>`:''}`:''}</span><div class="strategy-actions">${st.id.startsWith('loop')?'<a class="button" href="#loop">Strategien</a>':`<button data-view="${escape(st.id)}" aria-pressed="${st.id===selectedStrategy}">${st.id===selectedStrategy?'Prognosen sichtbar':'Prognosen ansehen'}</button>`}<button data-strategy="${escape(st.id)}" data-action="${st.entries_enabled?'pause':'resume'}" ${!s.configured?'disabled':''}>${st.entries_enabled?'Einstiege pausieren':'Einstiege einschalten'}</button></div></article>`);
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
  const testCell=t=>t?`<span class="pill ${t.gruen?'buy':'sell'}">${t.gruen?'GRÜN':'ROT'}</span><br><small class="muted">${pct(t.rendite)} · DD ${pct(t.max_drawdown)} · ${escape(t.trades)} Trades${t.methode==='walk_forward'?' · selbst angepasst, Test '+escape(t.zeitraum||''):''}</small>`:'—';
  $('loop-running').innerHTML=`<div class="table-wrap"><table><thead><tr><th>Broker / Aktie</th><th>Beste grüne Version / Besitzer</th><th>Einzeltest</th><th>Strategie-Signal</th><th>Entscheidung</th></tr></thead><tbody>${(l.stocks||[]).map(r=>`<tr><td><small>${r.broker==='trading212'?'Trading 212':'Alpaca'}</small><br>${escape(r.symbol)}${r.bot==='loop_day'?`<br><small class="muted">Intraday${r.timeframe?' · '+escape(r.timeframe):''}</small>`:''}</td><td>${escape(r.version||'—')}${r.owner?`<br><small>Besitzer: ${escape(r.owner)}</small>`:''}</td><td>${testCell(r.test)}</td><td>${signal(r.signal||'HOLD')}</td><td>${signal(r.action||'WAIT')}<br><small class="muted">${escape(r.reason)}</small><details><summary>Alle Versionen</summary>${Object.entries(r.views||{}).map(([v,i])=>`<div>${escape(v)}: ${escape(i.signal||i.error||'—')} · ${pct(i.test?.rendite)} · DD ${pct(i.test?.max_drawdown)} · ${i.test?.gruen?'GRÜN':'ROT'}</div>`).join('')}</details></td></tr>`).join('')||'<tr><td colspan="5" class="empty">Aktiviere getestete Versionen und gib dem Trading-Loop eine Gewichtung über 0.</td></tr>'}</tbody></table></div>` + l.running.map(v=>`<div class="loop-version section-title"><span><b>${escape(v.version)}</b> · ${v.green}/${v.rows.length} Einzeltests grün · ${v.computed_at?time(v.computed_at):'wird berechnet…'}${v.error?` · ${escape(v.error.message)}`:''}</span><button data-loop="deactivate" data-version="${escape(v.version)}">Abschalten</button></div>`).join('');
  $('loop-versions').innerHTML=l.versions.map(v=>{const r=v.report||{},[label,tone]=LOOP_STATUS[v.status]||[v.status,'hold'];return `<tr><td>${escape(v.version)}</td><td>${v.symbol==='LISTE'?`Liste (${escape(r.gruen_anzahl??'—')}/${escape(r.anzahl??'—')} grün)`:escape(v.symbol||'—')}<br><small class="muted">${v.bot==='loop_day'?`Intraday-Loop · ${escape(v.timeframe)}`:'Trading-Loop · Tageskerzen'}</small></td><td>${pct(r.rendite)}</td><td>${pct(r.kaufen_halten)}</td><td>${pct(r.max_drawdown)}</td><td>${escape(r.trades??'—')}</td><td><span class="pill ${tone}">${escape(label)}</span><br><small class="muted">${escape(v.note)}</small></td><td><div class="row-actions">${v.active?`<button data-loop="deactivate" data-version="${escape(v.version)}">Abschalten</button>`:v.tradable?`<button data-loop="activate" data-version="${escape(v.version)}">Einschalten</button>`:''}<button data-loop="retest" data-version="${escape(v.version)}" ${testing?'disabled':''}>Neu testen</button></div></td></tr>`;}).join('')||'<tr><td colspan="8" class="empty">Keine Strategie-Versionen gefunden.</td></tr>';
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
  $('notice').textContent=!s.configured?'Setup needed: add Alpaca keys and a dashboard password to .env, then restart.':s.account?.loss_halt?'Daily loss limit reached. New entries are disabled; protective exits remain active.':`${(parallel?s.portfolio_engine?.message:null) || s.engine.message || s.engine.status} · Alpaca (US): ${s.settings.feed.toUpperCase()} data, ${s.account?.market_open?'market open':'market closed'}${s.trading212?.enabled?` · Trading 212: ${s.trading212.entries_enabled?'entries on':'paused'}${s.trading212.ready_problem?' – '+s.trading212.ready_problem:` – ${s.trading212.engine?.message||s.trading212.engine?.status||''}`}`:''}`;
  $('memory').textContent=s.resources.available===false?'Telemetry unavailable':`${s.resources.process_memory_mb.toFixed(0)} MB / 6 GB`; $('memory-bar').value=s.resources.process_memory_mb;
  $('train-memory').textContent=`${(s.resources.training_memory_mb/1024).toFixed(1)} GB`;
  $('training-status').textContent=`${s.training.status}${s.training.symbol?' · '+s.training.symbol:''}${s.training.model?' / '+s.training.model:''}`;
  $('training-log').innerHTML=s.training_history.slice(0,12).map(e=>`<div class="log-row"><time>${time(e.at)}</time><span>${escape(e.symbol||'')} ${escape(e.model||'')} · ${escape(e.message)}</span></div>`).join('');
  $('orders').innerHTML=s.orders.map(o=>`<tr><td>${time(o.at)}</td><td><small>${o.broker==='trading212'?'Trading 212':'Alpaca'}</small><br>${escape(o.symbol)}</td><td>${escape(o.strategy||'daily')}</td><td>${signal(o.side.toUpperCase())}</td><td>${escape(o.qty)}</td><td>${escape(o.filled_qty??'—')}</td><td>${o.filled_avg_price?money(Number(o.filled_avg_price),o.currency||'USD'):'—'}</td><td>${escape(o.status)}${o.queued_for_open?`<br><small>Queued for ${escape(o.session)} / limit ${money(Number(o.limit_price),o.currency||'USD')}</small>`:o.limit_price?`<br><small>Limit ${money(Number(o.limit_price),o.currency||'USD')}</small>`:''}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">No orders submitted.</td></tr>';
  $('positions').innerHTML=(s.account?.positions||[]).map(p=>`<div class="position"><b>${escape(p.symbol)}${p.strategy?` / ${escape(p.strategy)}`:''}</b><span>${escape(p.qty)} shares</span><span>${money(Number(p.market_value))}</span><span>${money(Number(p.unrealized_pl))} unrealized</span></div>`).join('') || 'No open positions.';
  $('updated').textContent=`LAST UPDATED ${time(s.at)}`;
  renderMoney(s); renderTrading212(s); renderReadonly(s.readonly);
  drawChart(s.equity); renderModels(); renderCandles(); renderForecast(); if(s.loop) renderLoopChart(s);
}
async function refresh() {const serial=++refreshSerial;try{const r=await fetch('/api/state'+(selectedStrategy?'?strategy='+encodeURIComponent(selectedStrategy):''));if(!r.ok)throw Error(`Dashboard request failed (${r.status})`);const snapshot=await r.json();if(serial===refreshSerial)render(snapshot);}catch(e){$('connection').textContent='OFFLINE';$('notice').textContent=e.message+' · displayed information may be stale.';}}
async function control(action,strategy='all') {try{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-Bot-Control':'dashboard'},body:JSON.stringify({action,strategy})});const body=await r.json();if(!r.ok)throw Error(body.error||'Action failed');toast(body.warning||(action==='train'?'Retraining queued.':'Entry setting saved.'));await refresh();}catch(e){toast(e.message);}}
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
const PALETTE=['#6ca5da','#b69ad7','#d39462','#83b9a3','#c4a572','#df8d81','#9fc7c0','#e1a7c4'];
// The page's CSP forbids inline style attributes; SVG fill is a presentation attribute.
const swatch=color=>`<svg class="swatch" viewBox="0 0 14 4" aria-hidden="true"><rect width="14" height="4" rx="2" fill="${escape(color)}"/></svg>`;
const MODEL_COLORS={ridge:'#6ca5da',extra_trees:'#b69ad7',hist_gradient:'#d39462',ensemble:'#e5cb86',actual:'#f4f7f4'};
// Shared candlestick renderer: bars by index (no weekend/overnight gaps), optional lines, markers and a lower pane.
function candleSvg(o) {
  const bars=o.bars, n=bars.length+(o.extra||0), lines=o.lines||[], markers=o.markers||[];
  const upper=lines.filter(l=>!l.lower), lower=lines.filter(l=>l.lower&&l.points.length);
  const bottom=lower.length?290:370, lowTop=315, lowBottom=375;
  const values=bars.flatMap(b=>[b.low,b.high]).concat(upper.flatMap(l=>l.points.map(p=>p.v)),markers.map(m=>m.v)).filter(Number.isFinite);
  if(!values.length) return '';
  const lo=Math.min(...values),hi=Math.max(...values),pad=(hi-lo||hi*.01||1)*.06;
  const x=i=>60+(i+.5)/Math.max(1,n)*860, y=v=>bottom-(v-lo+pad)/(hi-lo+2*pad)*(bottom-20);
  const width=Math.max(1,Math.min(12,860/Math.max(1,n)*.7));
  let svg=`<svg viewBox="0 0 1000 410" role="img" aria-label="${escape(o.label||'Candlestick chart')}">`;
  for(let k=0;k<5;k++){const p=lo+(hi-lo)*k/4,yy=y(p);svg+=`<line class="chart-grid" x1="60" x2="930" y1="${yy}" y2="${yy}"/><text x="936" y="${yy+4}">${escape(o.currency===null?p.toFixed(2):money(p,o.currency||'USD'))}</text>`;}
  bars.forEach((b,i)=>{if(![b.open,b.high,b.low,b.close].every(Number.isFinite))return;const xx=x(i),color=b.close>=b.open?'#70b79e':'#df8d81';svg+=`<g><title>${escape(o.time(b.at))} · O ${b.open.toFixed(2)} H ${b.high.toFixed(2)} L ${b.low.toFixed(2)} C ${b.close.toFixed(2)}</title><line x1="${xx}" x2="${xx}" y1="${y(b.high)}" y2="${y(b.low)}" stroke="${color}"/><rect x="${xx-width/2}" y="${Math.min(y(b.open),y(b.close))}" width="${width}" height="${Math.max(1,Math.abs(y(b.open)-y(b.close)))}" fill="${color}"/></g>`;});
  if(o.extra) svg+=`<line class="chart-grid" x1="${x(bars.length-.5)}" x2="${x(bars.length-.5)}" y1="20" y2="${bottom}" stroke-dasharray="3 4"/><text x="${x(bars.length)}" y="${bottom+14}" text-anchor="middle">Ziel</text>`;
  const path=(points,yf)=>{let d='',last=null;for(const p of points){if(!Number.isFinite(p.v)){last=null;continue;}d+=`${last!==null&&p.i-last<=(p.gap||1)?'L':'M'}${x(p.i).toFixed(1)},${yf(p.v).toFixed(1)} `;last=p.i;}return d;};
  for(const l of upper) {svg+=`<path d="${path(l.points,y)}" fill="none" stroke="${l.color}" stroke-width="${l.width||1.5}" ${l.dash?`stroke-dasharray="${l.dash}"`:''} opacity="${l.opacity||1}"><title>${escape(l.name)}</title></path>`;if(l.dots)for(const p of l.points)if(Number.isFinite(p.v))svg+=`<circle cx="${x(p.i)}" cy="${y(p.v)}" r="${l.dots}" fill="${l.hollow?'none':l.color}" stroke="${l.color}"><title>${escape(p.title||l.name)}</title></circle>`;}
  if(lower.length){const lv=lower.flatMap(l=>l.points.map(p=>p.v)).filter(Number.isFinite),a=Math.min(...lv),b=Math.max(...lv)||a+1,ly=v=>lowBottom-(v-a)/((b-a)||1)*(lowBottom-lowTop);svg+=`<line class="chart-grid" x1="60" x2="930" y1="${lowTop}" y2="${lowTop}"/><text x="936" y="${lowTop+4}">${b.toFixed(1)}</text><text x="936" y="${lowBottom+4}">${a.toFixed(1)}</text>`;for(const l of lower)svg+=`<path d="${path(l.points,ly)}" fill="none" stroke="${l.color}" stroke-width="1.2"><title>${escape(l.name)}</title></path>`;}
  for(const m of markers){if(!Number.isFinite(m.v))continue;const xx=x(m.i),yy=y(m.v);const shape=m.kind==='buy'?`M${xx},${yy+4} l-6,11 h12 Z`:m.kind==='sell'?`M${xx},${yy-4} l-6,-11 h12 Z`:`M${xx},${yy-4.5} l4.5,4.5 l-4.5,4.5 l-4.5,-4.5 Z`;svg+=`<path d="${shape}" fill="${m.color||(m.kind==='buy'?'#b6df9c':m.kind==='sell'?'#f2998a':'#f4f7f4')}" ${m.hollow?'fill-opacity=".15" stroke="#f4f7f4"':''}><title>${escape(m.title||'')}</title></path>`;}
  const ticks=Math.min(6,bars.length);for(let k=0;k<ticks;k++){const i=Math.round(k*(bars.length-1)/Math.max(1,ticks-1));svg+=`<text x="${x(i)}" y="402" text-anchor="middle">${escape(o.tick(bars[i].at))}</text>`;}
  return svg+'</svg>';
}
const nyDay=t=>new Date(t).toLocaleDateString('de-DE',{timeZone:'America/New_York',day:'2-digit',month:'2-digit'});
const nyTime=t=>new Date(t).toLocaleTimeString('de-DE',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'});
// Largest bar index whose start is at or before t (fills, forecast references).
function barAt(times,t){let found=-1;for(let i=0;i<times.length;i++){if(times[i]<=t)found=i;else break;}return found;}

let forecastData=null, forecastKey='', forecastFetched=0, forecastSerial=0;
const forecastHidden=new Set();
async function renderForecast() {
  const strategy=state?.trading_mode, symbol=selected;
  const show=['daily','daytrade'].includes(strategy)&&symbol;
  $('forecast-panel').hidden=!show; if(!show) return;
  const key=strategy+':'+symbol;
  if(key===forecastKey&&Date.now()-forecastFetched<(strategy==='daytrade'?60000:300000)) return;
  forecastKey=key; forecastFetched=Date.now();
  const serial=++forecastSerial;
  $('forecast-title').textContent=`${symbol} · was jedes Modell vorhergesagt hat`;
  try {
    const r=await fetch(`/api/predictions/${encodeURIComponent(symbol)}?strategy=${encodeURIComponent(strategy)}`);
    const data=await r.json(); if(serial!==forecastSerial) return;
    if(!r.ok) throw Error(data.error||'Prognosedaten nicht verfügbar');
    forecastData=data; drawForecast();
  } catch(err) {if(serial===forecastSerial){forecastFetched=0;$('forecast-chart').textContent=err.message;$('forecast-status').textContent='NICHT VERFÜGBAR';}}
}
function drawForecast() {
  const d=forecastData, bars=d.bars||[], intraday=d.timeframe==='5Min';
  const models=Object.keys(d.names||{}).filter(id=>d.forecasts.some(f=>f.models[id]));
  $('forecast-legend').innerHTML=[...models,'actual'].map(id=>`<label><input type="checkbox" data-series="${escape(id)}" ${forecastHidden.has(id)?'':'checked'}>${swatch(MODEL_COLORS[id]||'#aaa')}${escape(id==='actual'?'Tatsächlicher Kurs am Ziel':d.names[id])}</label>`).join('')+'<span>▲ Kauf · ▼ Verkauf (Fills)</span>';
  $('forecast-status').textContent=intraday?'5-MIN-KERZEN':'TAGESKERZEN';
  if(!bars.length){$('forecast-chart').textContent='Noch keine Kerzen verfügbar.';$('forecast-table').innerHTML='';return;}
  const times=bars.map(b=>Date.parse(b.at)), last=times.at(-1);
  const step=intraday?300000:null;
  const target=t=>{if(t==null)return bars.length;const ms=Date.parse(t),exact=times.indexOf(ms);if(exact>=0)return exact;return ms>last&&step?bars.length-1+Math.round((ms-last)/step):barAt(times,ms);};
  const extra=d.forecasts.some(f=>f.status==='pending')?(intraday?Math.max(1,...d.forecasts.filter(f=>f.status==='pending').map(f=>target(f.target_at)-bars.length+1)):1):0;
  const lines=[];
  for(const id of models){if(forecastHidden.has(id))continue;const color=MODEL_COLORS[id]||'#aaa';
    lines.push({name:d.names[id],color,width:1.2,opacity:.85,dots:2.5,points:d.forecasts.filter(f=>f.status==='resolved'&&f.models[id]).map(f=>({i:target(f.target_at),v:f.models[id].price,gap:intraday?4:3,title:`${d.names[id]}: ${money(f.models[id].price)} (${percent(f.models[id].return)}) für ${intraday?nyTime(f.target_at):nyDay(f.target_at)}`}))});
    for(const f of d.forecasts.filter(f=>f.status==='pending'&&f.models[id])) lines.push({name:`${d.names[id]} (offen)`,color,dash:'5 4',dots:3,hollow:true,points:[{i:barAt(times,Date.parse(f.reference_at)),v:f.reference},{i:target(f.target_at),v:f.models[id].price,gap:99,title:`${d.names[id]}: ${money(f.models[id].price)} (${percent(f.models[id].return)}), noch offen`}]});}
  const markers=[];
  if(!forecastHidden.has('actual')) for(const f of d.forecasts.filter(f=>f.status==='resolved')) markers.push({i:target(f.target_at),v:f.actual,kind:'actual',title:`Tatsächlich ${money(f.actual)} (${percent(f.actual_return)}) · Bezug ${money(f.reference)}`});
  for(const fill of d.fills||[]) markers.push({i:barAt(times,Date.parse(fill.at)),v:fill.price,kind:fill.side==='buy'?'buy':'sell',title:`${fill.side==='buy'?'Kauf':'Verkauf'} ${fill.qty} zu ${money(fill.price)} · ${time(fill.at)}`});
  $('forecast-chart').innerHTML=candleSvg({bars,lines,markers,extra,label:'Kerzen mit den Prognosen jedes Modells und dem tatsächlichen Kurs',time:t=>intraday?nyTime(t):nyDay(t),tick:t=>intraday?nyTime(t):nyDay(t)});
  const resolved=d.forecasts.filter(f=>f.status==='resolved').length;
  $('forecast-note').textContent=`Punkte: Kurs, den jedes Modell für das Ziel vorhergesagt hat (Bezugskurs × (1 + vorhergesagte Rendite)); weiße Rauten: tatsächlicher Kurs. Horizont: ${d.horizon}. ${resolved} aufgelöste Prognosen im Bild, offene Prognosen gestrichelt${intraday?'':' (Bezug: letzter Schlusskurs, da die nächste Eröffnung noch unbekannt ist)'}. Nur gespeicherte Prognosen, nichts nachträglich berechnet.`;
  $('forecast-model-heads').outerHTML=`<th id="forecast-model-heads" colspan="1">${models.map(id=>escape(d.names[id])).join(' / ')}</th>`;
  $('forecast-table').innerHTML=[...d.forecasts].reverse().slice(0,8).map(f=>{const actual=f.status==='resolved'?f.actual_return:null;return `<tr><td>${escape(intraday?nyTime(f.bar_at):nyDay(f.bar_at))}${f.status==='pending'?' <small class="muted">offen</small>':''}</td><td>${money(f.reference)}</td><td>${models.map(id=>{const m=f.models[id];if(!m)return '—';const hit=actual==null?'':Math.sign(m.return)===Math.sign(actual)?'buy':'sell';return `<span class="${hit}">${percent(m.return)}</span>`;}).join(' / ')}</td><td>${actual==null?'—':`${money(f.actual)} (${percent(actual)})`}</td></tr>`;}).join('')||'<tr><td colspan="4" class="empty">Noch keine gespeicherten Prognosen für diese Aktie.</td></tr>';
}
$('forecast-legend').addEventListener('change',e=>{const id=e.target.dataset.series;if(!id||!forecastData)return;e.target.checked?forecastHidden.delete(id):forecastHidden.add(id);drawForecast();});

let loopChartSymbol=null, loopChartKey='', loopChartFetched=0, loopChartSerial=0;
async function renderLoopChart(s) {
  const stocks=(s.loop?.stocks||[]).map(r=>`${r.bot}:${r.symbol}`);
  if(!stocks.length){$('loop-chart-symbols').innerHTML='';$('loop-chart').textContent='Aktiviere getestete Versionen; danach erscheinen hier die Kerzen jeder Aktie.';return;}
  if(!stocks.includes(loopChartSymbol)) loopChartSymbol=stocks[0];
  $('loop-chart-symbols').innerHTML=stocks.map(x=>`<button data-loop-chart="${escape(x)}" class="${x===loopChartSymbol?'active':''}" aria-pressed="${x===loopChartSymbol}">${escape(x.split(':')[1])}${x.startsWith('loop_day:')?' · intraday':x.startsWith('trading212:')?' · T212':''}</button>`).join('');
  if(loopChartSymbol===loopChartKey&&Date.now()-loopChartFetched<300000) return;
  loopChartKey=loopChartSymbol; loopChartFetched=Date.now();
  const serial=++loopChartSerial, [bot,symbol]=loopChartSymbol.split(':');
  try {
    const r=await fetch(`/api/loop/chart/${encodeURIComponent(symbol)}?bot=${encodeURIComponent(bot)}`); const data=await r.json();
    if(serial!==loopChartSerial) return;
    if(!r.ok) throw Error(data.error||'Chartdaten nicht verfügbar');
    drawLoopChart(data);
  } catch(err){if(serial===loopChartSerial){loopChartFetched=0;$('loop-chart').textContent=err.message;$('loop-chart-status').textContent='NICHT VERFÜGBAR';$('loop-chart-legend').innerHTML='';$('loop-chart-note').textContent='';}}
}
function drawLoopChart(d) {
  const c=d.chart, bars=c.bars||[], t=d.test||{}, days=bars.map(b=>b.at);
  $('loop-chart-title').textContent=`${d.symbol} · ${d.version}${d.bot==='loop_day'?` · Intraday ${escape(t.zeitrahmen||'')}`:''}${d.deciding?'':' (nicht entscheidend)'}`;
  $('loop-chart-status').innerHTML=`${signal(d.signal||'HOLD')}`;
  const overlay=c.indicators.filter(i=>i.overlay), lower=c.indicators.filter(i=>!i.overlay).slice(0,3);
  const lines=[...overlay.map((ind,k)=>({name:ind.name,color:PALETTE[k%PALETTE.length],points:ind.values.map((v,i)=>({i,v}))})),
    ...lower.map((ind,k)=>({name:ind.name,lower:true,color:PALETTE[(overlay.length+k)%PALETTE.length],points:ind.values.map((v,i)=>({i,v}))}))];
  const markers=[];
  for(const tr of c.trades){const a=days.indexOf(tr.entry_at);if(a>=0)markers.push({i:a,v:tr.entry,kind:'buy',title:`Kauf ${tr.entry_at} zu ${money(tr.entry)}${tr.exit_at?'':' · noch offen'}`});
    const b=tr.exit_at?days.indexOf(tr.exit_at):-1;if(b>=0)markers.push({i:b,v:tr.exit,kind:'sell',title:`Verkauf ${tr.exit_at} zu ${money(tr.exit)} · ${percent(tr.return)}`});}
  $('loop-chart-legend').innerHTML=lines.map(l=>`<span>${swatch(l.color)} ${escape(l.name)}${l.lower?' (unten)':''}</span>`).join('')+'<span>▲ Kauf · ▼ Verkauf (Backtest mit den aktuellen Werten)</span>';
  const intraday=bars.some(b=>b.at.includes('T'));
  const tick=x=>intraday?`${x.slice(8,10)}.${x.slice(5,7)}. ${x.slice(11)}`:x.slice(5).split('-').reverse().join('.');
  $('loop-chart').innerHTML=bars.length?candleSvg({bars,lines,markers,currency:d.currency,label:intraday?'Intraday-Kerzen mit den Indikatoren und Trades der Strategie':'Tageskerzen mit den Indikatoren und Trades der Strategie',time:x=>x.replace('T',' '),tick}):'Keine Kerzen.';
  const params=Object.entries(t.parameter||{}).map(([k,v])=>`${k}=<b>${escape(v)}</b>${t.standard&&t.standard[k]!==v?` <span class="muted">(Standard ${escape(t.standard[k])})</span>`:''}`).join(' · ');
  const years=(t.jahre||[]).map(j=>`${intraday?`${j.von.slice(5)}–${j.bis.slice(5)}`:`${j.von.slice(0,4)}/${j.bis.slice(0,4)}`}: ${pct(j.rendite)} mit ${Object.entries(j.parameter).map(([k,v])=>`${k}=${v}`).join(', ')||'Standardwerten'}`).join(' · ');
  $('loop-chart-note').innerHTML=`${escape(d.strategy_reason||'')}. Entscheidung: ${signal(d.action)} <span class="muted">${escape(d.reason||'')}</span><br>${t.methode==='walk_forward'?`Selbst-Anpassung (Walk-forward)${t.angepasst?'':', Standardwerte blieben die besten'}. Test ${escape(t.zeitraum||'')} mit vorher gewählten Werten: ${pct(t.rendite)}, max. Drawdown ${pct(t.max_drawdown)}, ${escape(t.trades)} Trades → ${t.gruen?'GRÜN':'ROT'}${years?` (${escape(years)})`:''}.`:`Ohne Selbst-Anpassung (keine PARAMETER): Test ${pct(t.rendite)}, max. Drawdown ${pct(t.max_drawdown)}, ${escape(t.trades)} Trades → ${t.gruen?'GRÜN':'ROT'}.`}${params?`<br><span class="param-list">Live-Werte: ${params}</span>`:''} <span class="muted">· ${t.quelle==='bericht'?'aus dem täglichen Stresstest':'vom Bot selbst getestet'} · berechnet ${time(d.computed_at)}</span>`;
}
$('loop-chart-symbols').addEventListener('click',e=>{const b=e.target.closest('[data-loop-chart]');if(b){loopChartSymbol=b.dataset.loopChart;loopChartKey='';renderLoopChart(state);}});
function renderReadonly(r) {
  if(!r) return;
  $('readonly-state').textContent=r.enabled?`AN · ${r.used_today}/${r.limit} HEUTE`:r.problem?'AUS · TOKEN UNGÜLTIG':'AUS';
  $('readonly-note').innerHTML=r.enabled?`Ein externer Assistent (z. B. ein GPT mit Action) kann mit dem Token aus <code>READONLY_API_TOKEN</code> nur lesen: Zusammenfassung, einzelne Aktien, Trades. Er kann nichts kaufen, verkaufen oder einstellen. Schema für den GPT-Builder: <code>${escape(r.schema_url)}</code>. Höchstens ${escape(r.limit)} Anfragen pro Tag.`:r.problem?`<b class="sell">Aus, weil der Token in .env nicht passt:</b> ${escape(r.problem)}. Der Rest des Bots läuft normal. Neuen Token erzeugen: <code>python -c "import secrets; print(secrets.token_urlsafe(32))"</code>, in <code>.env</code> eintragen, neu starten.`:'Aus. Für einen GPT, der einmal am Tag nachschaut: <code>READONLY_API_TOKEN</code> in <code>.env</code> setzen (mindestens 32 zufällige Zeichen) und neu starten.';
  $('readonly-log').innerHTML=(r.recent||[]).map(e=>`<div class="log-row"><time>${time(e.at)}</time><span>${e.status===200?'':'<b class="sell">abgelehnt</b> · '}${escape(e.path)}${e.agent?` · <span class="muted">${escape(e.agent)}</span>`:''}</span></div>`).join('')||(r.enabled?'<p class="empty">Noch keine Zugriffe.</p>':'');
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
  $('t212-note').textContent=`${t.engine.message||t.engine.status} · Entries ${t.entries_enabled?'enabled':'paused'} · budget ${money(budget,code)} from this account only · ${t.allow_fx?'Foreign-currency orders allowed (FX fees may apply)':'Same-currency entries only'} · Kursdaten: ${t.data_source==='yahoo'?'Yahoo Finance (kostenlos)':'Twelve Data'+(t.data_configured?'':' – TWELVEDATA_API_KEY fehlt')} · ${t.mappings?.error?`Liste ${escape(t.mappings.file)}: ${escape(t.mappings.error)}`:`${t.mappings?.count??0} Werte aus ${escape(t.mappings?.file||'')} geladen`}${t.halt?' · DRAWDOWN STOP ACTIVE':''}`;
  if(t.ready_problem&&!t.entries_enabled) $('t212-note').textContent+=` · Noch nicht startklar: ${t.ready_problem}. Die Alpaca-Bots laufen davon unabhängig.`;
  $('t212-positions').innerHTML=(account?.positions||[]).map(p=>`<div class="position"><b>${escape(p.symbol)}</b><span>${escape(p.qty)} shares</span><span>${money(Number(p.market_value),p.currency)}</span><span>${money(Number(p.unrealized_pl),p.currency)} unrealized</span></div>`).join('')||'No open Trading 212 positions.';
}
async function trading212Control(action) {
  try {await api('/api/trading212/control',{action});toast('Trading 212 setting saved.');await refresh();} catch(err){toast(err.message);}
}
$('t212-toggle').addEventListener('click',()=>trading212Control(state?.trading212?.entries_enabled?'pause':'resume'));
$('t212-retest').addEventListener('click',()=>trading212Control('retest'));
$('t212-reset').addEventListener('click',()=>{if(confirm('Reset the Trading 212 drawdown peak? Entries will stay paused.'))trading212Control('reset_peak');});
refresh(); setInterval(refresh,5000);
