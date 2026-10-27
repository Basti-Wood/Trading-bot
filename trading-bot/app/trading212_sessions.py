"""Complete missing historical session dates without inventing broker opening hours."""
from functools import lru_cache

import exchange_calendars as calendars
import pandas as pd


@lru_cache(maxsize=32)
def calendar(mic, year):
    return calendars.get_calendar(mic, start=f"{year - 1}-01-01", end=f"{year + 1}-12-31")


def surrounding_sessions(mic, now):
    """Holiday/DST-aware reference dates; never authorizes an open broker session."""
    try:
        cal = calendar(mic, now.year)
        day = now.tz_convert(cal.tz).tz_localize(None).normalize()
        rows = cal.schedule.loc[day - pd.Timedelta(days=30):day + pd.Timedelta(days=30)]
        closed = rows.loc[rows.close <= now, "close"]
        future = rows.loc[rows.open > now, "open"]
        return {"prior_close": closed.iloc[-1] if len(closed) else None,
                "next_open": future.iloc[0] if len(future) else None}
    except (ValueError, KeyError, IndexError, calendars.errors.InvalidCalendarName):
        return {"prior_close": None, "next_open": None}
