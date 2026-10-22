"""Dashboard data from completed session bars; no chart code can place orders."""
import math
import numpy as np
import pandas as pd
from .daytrade import NY, OPEN_MINUTE, regular


def finite(value):
    return float(value) if value is not None and math.isfinite(float(value)) else None


def session_bars(frame, now):
    if frame is None or frame.empty:
        return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
    now = pd.Timestamp(now)
    frame = regular(frame).sort_index()
    frame = frame[~frame.index.duplicated(keep='last')]
    keep = ((frame.index.tz_convert(NY).date == now.tz_convert(NY).date())
            & (frame.index + pd.Timedelta(minutes=1) <= now))
    return frame.loc[keep].tail(390)


def indicator_series(bars, base=None):
    """Same formulas and five-minute noise offsets as the trading playbook."""
    if bars.empty:
        return {k: [] for k in ('vwap', 'ema9', 'ema20', 'opening_high', 'opening_low', 'noise_upper', 'noise_lower')}
    typical = (bars.high + bars.low + bars.close) / 3
    volume = bars.volume.cumsum().replace(0, np.nan)
    vwap = ((typical * bars.volume).cumsum() / volume).ffill().fillna(typical)
    local = bars.index.tz_convert(NY)
    starts = local.hour * 60 + local.minute
    opened = int(starts[0]) == OPEN_MINUTE
    opening = bars[starts < OPEN_MINUTE + 5]
    high = finite(opening.high.max()) if opened else None
    low = finite(opening.low.min()) if opened else None
    # A completed five-minute opening range must never appear on earlier bars.
    highs = [high if opened and m >= OPEN_MINUTE + 4 else None for m in starts]
    lows = [low if opened and m >= OPEN_MINUTE + 4 else None for m in starts]
    upper, lower = [], []
    base = base if base and not base.get('error') else None
    for m in starts:
        clock = int(m) + 1 - OPEN_MINUTE
        sigma = (base.get('sigma') or {}).get(str(max(5, clock // 5 * 5))) if base else None
        known = base and opened and clock >= 5 and sigma is not None
        upper.append(finite(max(float(bars.open.iloc[0]), base['prev_close']) * (1 + sigma)) if known else None)
        lower.append(finite(min(float(bars.open.iloc[0]), base['prev_close']) * (1 - sigma)) if known else None)
    return {'vwap': [finite(v) for v in vwap],
            'ema9': [finite(v) for v in bars.close.ewm(span=9, adjust=False).mean()],
            'ema20': [finite(v) for v in bars.close.ewm(span=20, adjust=False).mean()],
            'opening_high': highs, 'opening_low': lows, 'noise_upper': upper, 'noise_lower': lower}


def snapshot(symbol, frame, base, now, previous=None, book=None):
    now = pd.Timestamp(now)
    day = str(now.tz_convert(NY).date())
    bars = session_bars(frame, now)
    if base and base.get("day") != day:
        base = None
    signals = list((previous or {}).get('signals', [])) if (previous or {}).get('day') == day else []
    if book and book.get('bar_at') in {at.isoformat() for at in bars.index}:
        for signal in book.get('signals', []):
            item = {**signal, 'bar_at': book['bar_at']}
            if not any(s['bar_at'] == item['bar_at'] and s['strategy'] == item['strategy'] for s in signals):
                signals.append(item)
    return {'symbol': symbol, 'day': day, 'timezone': NY, 'timeframe': '1Min', 'at': now.isoformat(),
            'bars': [{'at': at.isoformat(), **{k: finite(row[k]) for k in ('open', 'high', 'low', 'close', 'volume')}}
                     for at, row in bars.iterrows()],
            'indicators': indicator_series(bars, base), 'signals': signals,
            'baseline': 'ready' if base and not base.get('error') and base.get('day') == day else 'unavailable'}


def forecast_overlay(prediction, now, closing=None):
    """Expose an actual current 15-minute model forecast, never extrapolate stale predictions."""
    try:
        if prediction.get('trading_mode') != 'daytrade':
            return None
        bar = pd.Timestamp(prediction['bar_at'])
        now = pd.Timestamp(now)
        start = bar + pd.Timedelta(minutes=5)
        end = start + pd.Timedelta(minutes=15)
        if (bar.tzinfo is None or bar.tz_convert(NY).date() != now.tz_convert(NY).date()
                or not 0 <= (now - start).total_seconds() <= 90
                or closing is not None and end > pd.Timestamp(closing)):
            return None
        close, change = finite(prediction['last_close']), finite(prediction['expected_return'])
        if not close or change is None or change <= -1:
            return None
        return {'from': start.isoformat(), 'to': end.isoformat(), 'price': close,
                'expected_return': change, 'target_price': close * (1 + change),
                'eligible': bool(prediction.get('eligible')), 'horizon_minutes': 15,
                'models': [{'name': m.get('name', m.get('id')), 'expected_return': finite(m['expected_return'])}
                           for m in prediction.get('models', [])]}
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
