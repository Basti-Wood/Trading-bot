# Positionsgröße, Marktlage und Verlustpause

This upgrade borrows four ideas from [claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) (MIT licence, TraderMonty). Its skills are workflows for a human trader; here they are re-implemented as small, automatic rules inside the bot. No code from that repository runs in the bot, and it needs no API key.

| Skill | What the bot now does | Code |
|---|---|---|
| position-sizer | Percentage budgets, risk per trade up to the stop, half Kelly as an upper limit, portfolio heat; the strictest limit wins | `app/sizing.py` |
| backtest-expert | Every stress test also reports win rate, average win/loss, profit factor, expectancy, a test with **doubled costs** and a quality score of 0–100 | `trading-loop/qualitaet.py`, `stresstest.py` |
| exposure-coach / uptrend-analyzer | Market posture from the benchmark's trend and the breadth of the bot's own stocks caps how much may be invested | `app/regime.py` |
| drawdown-circuit-breaker | After 3 losing trades in a row a bot pauses new buys for 24 hours | `app/streak.py` |

Green or red is unchanged: `urteil()` in `stresstest.py` and the values in `risiko.py` still decide. The new numbers only change **how much** is bought, and whether a buy happens at all when the strategy's own trades lose on average.

## How much one buy may spend

Everything is a percentage of the **bot's share** of the money: account value × bot budget (%) × the bot's weight. Trading 212 uses its own account, split between prediction models and loop by `TRADING212_PREDICTION_SHARE`. The dashboard (*Budget & Gewichtung*) sets the four percentages without a restart; they apply to Alpaca and Trading 212 alike.

| Limit | Default | Meaning |
|---|---|---|
| Einsatz pro Aktie | 10 % (`MAX_POSITION_FRACTION`) | At most this much in one stock |
| Höchstens investiert | 30 % (`MAX_GROSS_FRACTION`) | At most this much of the bot's share invested at the same time |
| Risiko pro Trade | 1 % (`RISK_PER_TRADE`) | If the stop-loss is hit, the loss is at most this share: amount ≤ share × 1 % ÷ stop distance |
| Offenes Risiko | 6 % (`PORTFOLIO_HEAT`) | All of the bot's open positions together risk at most this much down to their stops |

With a 5 % stop, 1 % risk allows 20 % in one stock, so the 10 % cap decides. A strategy with a 15 % stop is cut to 6.7 %. For example, a $100,000 account with the Trading-Loop at 50 % gives the loop $50,000. One stock gets at most $5,000, and $15,000 may be invested at once.

**Trading-Loop only (switchable):** the cap per stock is multiplied by 0.5 + quality ÷ 100, kept between 0.5× and 1.25×. Half Kelly from the test's win rate and average win/loss is the share of the budget to *risk*. Divided by the stop distance, it is an upper limit for the position. A strategy whose tested trades lose on average has a negative half Kelly, so it does not buy that stock, even when its overall test is green. In a simulation on 20 European listings with grid_v1, this held back 4 green stocks whose average trade was negative and turned into a loss with doubled costs. Without test statistics (old reports, prediction and day-trading bots) the factor is 1.

The decision for each stock is stored as `sizing:<symbol>`: amount, binding limit and all limits. The dashboard and the Trading 212 table show which limit decided.

## Market posture

| Posture | Rule | Usable share of "Höchstens investiert" |
|---|---|---|
| Angriff | Benchmark above its 200-day average with the 50-day above the 200-day, and ≥ 50 % of the bot's stocks above their 50-day average | 100 % |
| Vorsicht | Anything mixed | 60 % (`REGIME_CAUTION_SHARE`) |
| Defensive | Benchmark below its 200-day with the 50-day below the 200-day, and < 40 % of stocks above their 50-day average | 30 % (`REGIME_DEFENSIVE_SHARE`) |

The benchmarks are `MARKET_BENCHMARK` (SPY) for Alpaca and `TRADING212_BENCHMARK` for Trading 212. The Trading 212 benchmark is EXS1d_EQ, the DAX ETF, and must be in the instruments file. The Alpaca posture is refreshed every 4 hours. The Trading 212 posture is refreshed with every daily-data load. A posture older than 4 days, or with no data, does not restrict anything. The posture never sells: open positions keep their exits and stops.

## Losing-streak pause

When a bot's position closes, its last observed profit or loss is recorded as a trade result. After `COOLDOWN_LOSING_TRADES` (3) losing trades in a row, that bot buys nothing for `COOLDOWN_HOURS` (24) after the latest loss. A winning trade ends the streak. `COOLDOWN_LOSING_TRADES=0` switches the pause off. Every bot and both Trading 212 books count separately. The recorded result is the last seen unrealised P&L before the position disappeared, so it is close to the real result but not exact (final fill, fees).

## Stress-test quality score

`stresstest.py` adds a block per stock: `statistik` (trades, win rate, average win/loss, profit factor, expectancy per trade), `rendite_doppelte_kosten` and `qualitaet`. The score has five areas of 20 points each:

- **Stichprobe:** number of trades; under 30 trades gives at most 6 points.
- **Erwartung:** expected gain per trade.
- **Risiko:** drawdown and profit factor.
- **Robustheit:** years tested and how many parameters were tuned.
- **Ausführung:** whether the strategy is still in profit with doubled trading costs.

70 or more points is "stark", 40–69 "mittel", below 40 "schwach". Reports written before this change have no score and keep the neutral size. Click "Alle neu testen" to recompute them.
