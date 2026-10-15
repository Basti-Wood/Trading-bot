"""Rule-based day-trading playbook on completed one-minute regular-session bars.

Each setup carries its own entry, stop and exit; none needs a trained model.

* orb: five-minute opening-range breakout for "stocks in play" (Zarattini, Barbon &
  Aziz, 2024): green opening range, opening volume at least its 14-day norm,
  daily ATR >= $0.50, stop 10% of daily ATR below entry, held to the close.
* noise_breakout: intraday momentum beyond the noise boundary (Zarattini, Aziz &
  Barbon, "Beat the Market", 2024): upper band = max(open, previous close) x
  (1 + 14-day average absolute move from the open at this time of day); the exit
  trails at max(band, VWAP).
* vwap_pullback: trend-day pullback to a rising VWAP that reclaims the prior
  bar's high, with EMA 9 above EMA 20; 2R target.
* vwap_reversion: a stretch beyond 2 standard deviations below VWAP with RSI
  under 35 and a reversal bar; exits back at VWAP.
"""
from datetime import datetime, time
import numpy as np
import pandas as pd

NY = "America/New_York"
OPEN_MINUTE = 9 * 60 + 30
MIN_STOP = .002          # never tighter than normal bid/ask noise
RUNNER_TARGET = .08      # far bracket cap for trades managed by their own exits
TREND_EXITS = {"eod", "noise_trail"}
NAMES = {"orb": "Opening-range breakout", "noise_breakout": "Noise-boundary momentum",
         "vwap_pullback": "VWAP trend pullback", "vwap_reversion": "VWAP 2σ mean reversion"}


def session_open(day):
    return pd.Timestamp(datetime.combine(day, time(9, 30)), tz=NY)


def regular(frame):
    if frame is None or frame.empty:
        return frame
    local = frame.index.tz_convert(NY)
    minutes = local.hour * 60 + local.minute
    return frame[(minutes >= OPEN_MINUTE) & (minutes < 16 * 60)]


def true_range(frame):
    previous = frame.close.shift()
    return pd.concat([frame.high - frame.low, (frame.high - previous).abs(), (frame.low - previous).abs()], axis=1).max(axis=1)


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    loss = -delta.clip(upper=0).ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
    return float(100 * gain / (gain + loss)) if gain + loss > 0 else 50.


def baseline(daily, intraday, today):
    """Once-per-day inputs from completed prior sessions only."""
    if daily is None or intraday is None:
        raise ValueError("No history for opening-range and noise baselines")
    daily = daily[daily.index.tz_convert(NY).date < today].tail(15)
    if len(daily) < 15:
        raise ValueError("Fewer than 15 completed daily bars")
    intraday = regular(intraday)
    intraday = intraday[intraday.index.tz_convert(NY).date < today]
    opening, moves = [], {}
    for _, bars in list(intraday.groupby(intraday.index.tz_convert(NY).date))[-14:]:
        local = bars.index.tz_convert(NY)
        if local[0].hour * 60 + local[0].minute != OPEN_MINUTE:
            continue
        opening.append(float(bars.volume.iloc[0]))
        start = float(bars.open.iloc[0])
        # Five-minute bars are stamped at their start; the move is measured at the bar's close.
        for offset, close in zip(local.hour * 60 + local.minute - OPEN_MINUTE + 5, bars.close):
            moves.setdefault(int(offset), []).append(abs(float(close) / start - 1))
    if len(opening) < 5:
        raise ValueError("Fewer than five prior sessions with an opening bar")
    return {"day": str(today), "atr": float(true_range(daily).tail(14).mean()),
            "prev_close": float(daily.close.iloc[-1]), "opening_volume": float(np.mean(opening)),
            "sigma": {str(k): float(np.mean(v)) for k, v in moves.items() if len(v) >= 5}}


def signal(strategy, entry, stop, target, exit_rule, reason, cost):
    """Normalize stop/target; refuse setups whose reward cannot pay round-trip costs."""
    stop = min(stop, entry * (1 - MIN_STOP))
    if target is not None and (target - entry) / entry < 3 * cost:
        return None
    return {"strategy": strategy, "name": NAMES[strategy], "entry": entry, "stop": float(stop),
            "target": None if target is None else float(target), "exit": exit_rule, "reason": reason}


def playbook(bars, base, now, config):
    """Evaluate every setup on today's completed one-minute bars."""
    bars = regular(bars)
    if bars is None or len(bars) < 5:
        return {"signal": "HOLD", "signals": [], "at": now.isoformat(), "bar_at": None,
                "note": "Waiting for at least five one-minute bars this session"}
    local = bars.index.tz_convert(NY)
    starts = local.hour * 60 + local.minute
    clock = int(starts[-1]) + 1 - OPEN_MINUTE   # minutes since the open at the last bar's close
    last, prev = bars.iloc[-1], bars.iloc[-2]
    close = float(last.close)
    opened = int(starts[0]) == OPEN_MINUTE
    day_open = float(bars.open.iloc[0]) if opened else None
    typical = (bars.high + bars.low + bars.close) / 3
    volume = bars.volume.cumsum().replace(0, np.nan)
    vwaps = ((typical * bars.volume).cumsum() / volume).ffill().fillna(typical)
    vwap = float(vwaps.iloc[-1])
    deviation = float(np.sqrt((((typical - vwaps) ** 2) * bars.volume).cumsum() / volume).fillna(0).iloc[-1])
    fast = float(bars.close.ewm(span=9, adjust=False).mean().iloc[-1])
    slow = float(bars.close.ewm(span=20, adjust=False).mean().iloc[-1])
    strength = rsi(bars.close)
    atr = float(true_range(bars).tail(14).mean())
    cost = config.cost_bps / 10000 * 2
    base = base if base and not base.get("error") else None
    indicators = {"close": close, "vwap": vwap, "vwap_sd": deviation, "ema_fast": fast, "ema_slow": slow,
                  "rsi": strength, "atr_1m": atr, "minutes_open": clock}
    found = []

    if base and opened and 5 <= clock <= 120:
        opening = bars[starts < OPEN_MINUTE + 5]
        high, low = float(opening.high.max()), float(opening.low.min())
        relative = float(opening.volume.sum()) / base["opening_volume"] if base["opening_volume"] > 0 else 0.
        green = float(opening.close.iloc[-1]) > float(opening.open.iloc[0])
        # One trade per stock and day, on the first close above the range (as in the paper).
        first = bool((bars.close.iloc[len(opening):-1] <= high).all())
        indicators.update(opening_high=high, opening_low=low, opening_relative_volume=relative)
        # Enter near the breakout level only; a late chase has a worse stop.
        if (green and first and relative >= 1 and base["atr"] >= .5 and close >= 5
                and high < close <= high + .1 * base["atr"]):
            found.append(signal("orb", close, close - .1 * base["atr"], None, "eod",
                                f"Broke the green 5-minute range high {high:.2f} on {relative:.1f}× opening volume", cost))

    sigma = base and base["sigma"].get(str(max(5, clock // 5 * 5)))
    if sigma and opened:
        upper = max(day_open, base["prev_close"]) * (1 + sigma)
        indicators["noise_upper"] = upper
        # The paper only acts on half-hour marks, which filters minute-to-minute whipsaws.
        if 30 <= clock <= 360 and clock % 30 == 0 and close > upper and close > vwap and fast > slow:
            found.append(signal("noise_breakout", close, max(upper, vwap), None, "noise_trail",
                                f"Closed above the noise boundary {upper:.2f} and VWAP", cost))

    if 15 <= clock <= 360 and len(bars) >= 20:
        rising = vwap > float(vwaps.iloc[-16])
        touched = float(bars.low.iloc[-4:-1].min()) <= vwap * 1.001
        reclaimed = close > vwap and close > float(prev.high) and close > float(last.open)
        if fast > slow and rising and touched and reclaimed:
            stop = min(float(bars.low.iloc[-4:].min()), vwap) - .5 * atr
            found.append(signal("vwap_pullback", close, stop, close + 2 * (close - stop), "target",
                                "Pulled back to a rising VWAP and reclaimed the prior bar's high", cost))

    if 30 <= clock <= 330 and deviation > 0:
        lower = vwap - 2 * deviation
        indicators["vwap_lower_2sd"] = lower
        stretched = float(bars.low.iloc[-3:].min()) < lower and close < vwap - deviation
        reversal = close > float(last.open) and close > float(prev.close)
        if stretched and reversal and strength < 35:
            # Stop beyond the extreme by a full one-minute ATR so ordinary noise cannot trip it.
            stop = float(bars.low.iloc[-5:].min()) - atr
            if vwap - close >= 1.5 * (close - stop):
                found.append(signal("vwap_reversion", close, stop, vwap, "vwap_target",
                                    f"Reversal bar after a stretch below VWAP −2σ ({lower:.2f}), RSI {strength:.0f}", cost))

    found = [s for s in found if s and s["strategy"] in config.daytrade_strategies]
    return {"signal": "BUY" if found else "HOLD", "signals": found, "indicators": indicators,
            "at": now.isoformat(), "bar_at": bars.index[-1].isoformat(),
            "baseline": "ready" if base else "unavailable; opening-range and noise setups paused"}


def fresh(book, now):
    """Signals must come from a one-minute bar that closed within the last two minutes."""
    try:
        age = (now - pd.Timestamp(book["bar_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return 60 <= age <= 180


def best(book):
    # Earlier strategies have published evidence; prefer them when several fire.
    order = list(NAMES)
    return min(book["signals"], key=lambda s: order.index(s["strategy"]))


def bracket(config, plan):
    """Broker-side stop and take-profit as fractions of the entry reference."""
    entry = plan["entry"]
    stop = min(config.daytrade_stop_loss, max(MIN_STOP, (entry - plan["stop"]) / entry))
    target = (plan["target"] - entry) / entry if plan.get("target") else RUNNER_TARGET
    return stop, min(.10, max(.003, target))


def exit_signal(plan, price, indicators):
    """Strategy-specific exits; brackets at the broker remain the hard backstop."""
    if price is not None and plan.get("stop") and price <= plan["stop"]:
        return f"{plan['name']} stop reached"
    rule = plan.get("exit")
    if (rule == "noise_trail" and indicators.get("noise_upper") and indicators.get("vwap")
            and indicators.get("minutes_open", 0) % 30 == 0):
        trail = max(indicators["noise_upper"], indicators["vwap"])
        # Completed bars, not ticks, decide momentum exits to avoid intrabar whipsaws.
        if indicators["close"] < trail:
            return f"Momentum faded: closed below the noise boundary/VWAP trail {trail:.2f}"
    if rule == "vwap_target" and indicators.get("vwap") and price is not None and price >= indicators["vwap"]:
        return "Price reverted to VWAP; mean-reversion target reached"
    if rule == "target" and plan.get("target") and price is not None and price >= plan["target"]:
        return f"{plan['name']} target reached"
    return None
