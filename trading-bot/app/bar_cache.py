"""Recent bars per symbol, fetched in full once per day and then only from the last cached bar on.

Without it the Intraday-Loop reloaded 90 days of bars for every stock after every bar (and the
day-trading playbook a whole day of 1-minute bars every 30 seconds): about 200,000 bars per round,
enough to hit the data API's page limit and leave the bot without data.
The full reload each day keeps split/dividend adjustments ("adjustment": "all") consistent.
"""
import pandas as pd


class BarCache:
    def __init__(self):
        self.key, self.frames = None, {}

    def get(self, fetch, symbols, timeframe, start, now, day):
        """fetch(symbols, timeframe, start, end=...) -> {symbol: frame}, e.g. Alpaca.multi_bars."""
        if (timeframe, day) != self.key:
            self.key, self.frames = (timeframe, day), {}
        self.frames = {s: f for s, f in self.frames.items() if s in symbols}
        cached = [s for s in symbols if s in self.frames]
        missing = [s for s in symbols if s not in self.frames]
        if missing:
            self.frames.update({s: f for s, f in fetch(missing, timeframe, start, end=now).items() if len(f)})
        if cached:
            # Refetch from the oldest "last bar": it may have been incomplete when it was cached.
            since = min(self.frames[s].index[-1] for s in cached)
            for symbol, new in fetch(cached, timeframe, since, end=now).items():
                if symbol in self.frames and len(new):
                    merged = pd.concat([self.frames[symbol], new])
                    self.frames[symbol] = merged[~merged.index.duplicated(keep="last")].sort_index()
        return {s: f[f.index >= start] for s, f in self.frames.items()}
