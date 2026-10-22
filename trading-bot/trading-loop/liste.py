"""Die Aktienliste der Bots plus externe Aktien aus dem gemeinsamen Konto.

Nur lesender Zugriff auf die Datenbank. SYMBOL = "LISTE" testet jede Aktie einzeln.
"""
import datetime as dt
import json
import os
import re
import sqlite3
from pathlib import Path

ORDNER = Path(__file__).resolve().parent
BOT_DB = Path(os.getenv("TRADING_BOT_DB", ORDNER.parent / "data" / "bot.sqlite3"))
BOTS = {"daily", "daytrade", "loop"}
AKTIE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


def _lesen(con, key):
    zeile = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(zeile[0]) if zeile else None


def _besitzer(con, symbol):
    besitzer = _lesen(con, f"position_owner:{symbol}")
    if besitzer:
        return besitzer
    return "daily" if _lesen(con, f"owned:{symbol}") else None


def _verbinden():
    if not BOT_DB.is_file():
        raise SystemExit(f"Datenbank des Trading-Bots nicht gefunden ({BOT_DB}). Ist der Bot schon einmal gelaufen?")
    return sqlite3.connect(f"{BOT_DB.as_uri()}?mode=ro", uri=True, timeout=10)




def symbole():
    """(Liste, eigene Aktien, Stand des Scans). Bricht mit einer verständlichen Meldung ab, wenn nichts da ist."""
    con = _verbinden()
    try:
        universum = _lesen(con, "universe") or {}
        auswahl = universum.get("symbols") or []
        if not auswahl:
            # Feste Watchlist statt Markt-Scan: die Bots beobachten diese Symbole.
            auswahl = [*(_lesen(con, "monitor_symbols") or []), *(_lesen(con, "daytrade:monitor_symbols") or [])]
        positionen = (_lesen(con, "account") or {}).get("positions", [])
        eigene = [p["symbol"] for p in positionen if p.get("asset_class", "us_equity") == "us_equity"
                  and float(p.get("qty") or 0) > 0
                  and _besitzer(con, p["symbol"]) not in BOTS]
    finally:
        con.close()
    liste = [s for s in dict.fromkeys([*auswahl, *eigene]) if AKTIE.fullmatch(s)]
    if not liste:
        raise SystemExit("Die Aktienliste der Bots ist leer. Starte den Trading-Bot, damit der Scanner sie füllt.")
    return liste, [s for s in eigene if s in liste], universum.get("at")


def _zeit(stand):
    return dt.datetime.fromisoformat(stand).astimezone().strftime("%d.%m.%Y %H:%M") if stand else "unbekannt"


if __name__ == "__main__":
    liste, eigene, stand = symbole()
    print(f"{len(liste)} Aktien (Scan vom {_zeit(stand)}):")
    print(", ".join(f"{s} (deine)" if s in eigene else s for s in liste))
