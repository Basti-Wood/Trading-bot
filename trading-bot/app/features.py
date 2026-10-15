"""Only trailing observations; target is next open to the following open."""
import numpy as np
import pandas as pd

FEATURE_SCHEMA = 1

FEATURES = ["return_1d", "return_5d", "return_20d", "trend_10d", "trend_50d",
            "volatility_20d", "range_1d", "volume_ratio", "rsi_14d"]


def features(bars):
    c, v = bars.close, bars.volume
    returns = c.pct_change()
    gain = c.diff().clip(lower=0).rolling(14).mean()
    loss = -c.diff().clip(upper=0).rolling(14).mean()
    rsi = gain / (gain + loss).replace(0, np.nan)
    return pd.DataFrame({
        "return_1d": returns, "return_5d": c.pct_change(5), "return_20d": c.pct_change(20),
        "trend_10d": c / c.rolling(10).mean() - 1, "trend_50d": c / c.rolling(50).mean() - 1,
        "volatility_20d": returns.rolling(20).std(), "range_1d": (bars.high - bars.low) / c,
        "volume_ratio": v / v.rolling(20).mean().replace(0, np.nan), "rsi_14d": rsi.fillna(.5),
    }).replace([np.inf, -np.inf], np.nan).dropna()


def dataset(bars, intraday=False):
    x = features(bars)
    if intraday:
        # Three contiguous five-minute bars; no overnight or missing-bar labels.
        stamps = pd.Series(bars.index, index=bars.index)
        end = stamps.shift(-3)
        same_session = pd.Series(bars.index.tz_convert("America/New_York").date, index=bars.index)
        valid = (end - stamps == pd.Timedelta(minutes=15)) & (same_session == same_session.shift(-3))
        out = x.join((bars.close.shift(-3) / bars.close - 1).rename("target"))
        out["entry"] = bars.close
        out["low_next"] = pd.concat([bars.low.shift(-i) for i in (1, 2, 3)], axis=1).min(axis=1)
        out["high_next"] = pd.concat([bars.high.shift(-i) for i in (1, 2, 3)], axis=1).max(axis=1)
        out["label_end"] = end + pd.Timedelta(minutes=5)
        return out.loc[valid.reindex(out.index)].dropna()
    target = bars.open.shift(-2) / bars.open.shift(-1) - 1
    out = x.join(target.rename("target")).dropna()
    out["entry"] = bars.open.shift(-1).reindex(out.index)
    out["low_next"] = bars.low.shift(-1).reindex(out.index)
    out["high_next"] = bars.high.shift(-1).reindex(out.index)
    out["label_end"] = pd.Series(bars.index, index=bars.index).shift(-2).reindex(out.index)
    return out
