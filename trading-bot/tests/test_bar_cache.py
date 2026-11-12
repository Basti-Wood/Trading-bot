"""Bars are fetched in full once per day, then only from the last cached bar on."""
import unittest

import pandas as pd

from app.bar_cache import BarCache

DAY = pd.Timestamp("2026-11-12")


def bars(start, count):
    index = pd.date_range(start, periods=count, freq="5min", tz="UTC")
    return pd.DataFrame({"open": 1., "high": 1., "low": 1., "close": range(count), "volume": 1.}, index=index)


class BarCacheTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.data = {"AAA": bars("2026-11-12 14:30", 10), "BBB": bars("2026-11-12 14:30", 8)}

        def fetch(symbols, timeframe, start, end=None):
            self.calls.append((tuple(symbols), start))
            return {s: f[(f.index >= start) & (f.index <= end)] for s, f in self.data.items() if s in symbols}
        self.fetch, self.cache = fetch, BarCache()
        self.start = pd.Timestamp("2026-09-01", tz="UTC")

    def get(self, symbols, now, day=DAY.date()):
        return self.cache.get(self.fetch, symbols, "5Min", self.start, pd.Timestamp(now, tz="UTC"), day)

    def test_second_round_fetches_only_from_the_last_cached_bar(self):
        first = self.get(["AAA", "BBB"], "2026-11-12 15:05")
        self.assertEqual(len(first["AAA"]), 8)
        self.data["AAA"].iloc[7, self.data["AAA"].columns.get_loc("close")] = 99.  # the last bar was still forming
        second = self.get(["AAA", "BBB"], "2026-11-12 15:20")
        self.assertEqual(self.calls[1][1], pd.Timestamp("2026-11-12 15:05", tz="UTC"))  # oldest last bar (both end at 15:05)
        self.assertEqual(len(second["AAA"]), 10)
        self.assertEqual(second["AAA"]["close"].iloc[7], 99.)
        self.assertFalse(second["AAA"].index.duplicated().any())

    def test_new_day_or_new_symbol_loads_in_full_and_dropped_symbols_leave(self):
        self.get(["AAA"], "2026-11-12 15:05")
        self.get(["AAA", "BBB"], "2026-11-12 15:10")
        self.assertIn((("BBB",), self.start), self.calls)
        result = self.get(["BBB"], "2026-11-12 15:20")
        self.assertEqual(set(result), {"BBB"})
        self.get(["BBB"], "2026-11-13 15:20", day=pd.Timestamp("2026-11-13").date())
        self.assertEqual(self.calls[-1], (("BBB",), self.start))


if __name__ == "__main__":
    unittest.main()
