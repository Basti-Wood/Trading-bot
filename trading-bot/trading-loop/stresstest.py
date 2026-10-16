"""Stresstest: eine Strategie-Version über die letzten 5 Jahre Tageskerzen im Backtest.

  python stresstest.py strategien/<name>_v1.py

Die Strategie-Datei definiert SYMBOL (z. B. "XYZ" für eine Aktie, "BTC/USD" für Krypto oder "LISTE" für die
Aktienliste der Bots plus die eigenen Aktien, siehe liste.py) und eine Klasse Strategie(backtesting.Strategy).
Bei "LISTE" wird jede Aktie einzeln getestet; grün, wenn mindestens die Hälfte der Aktien grün ist. Kurse kommen von Alpaca (Aktien: IEX-Daten mit den Schlüsseln aus .env,
Krypto: ohne Schlüssel). Ergebnis, Trades und Chart landen in berichte/. Grün oder rot steht in der letzten Zeile.
"""
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import sys
import warnings
from pathlib import Path

import pandas as pd
from backtesting import Backtest
from dotenv import load_dotenv

import risiko

ORDNER = Path(__file__).resolve().parent
JAHRE = 5
STARTKAPITAL = 100_000
KOSTEN = 0.001          # 0,1 % pro Order als Puffer für Spread und Gebühren
MIN_TRADES = 10


def lade_strategie(pfad):
    spec = importlib.util.spec_from_file_location(pfad.stem, pfad)
    modul = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ORDNER))
    spec.loader.exec_module(modul)
    if not hasattr(modul, "SYMBOL") or not hasattr(modul, "Strategie"):
        raise SystemExit(f"{pfad.name} braucht SYMBOL und die Klasse Strategie.")
    return modul


def lade_kurse(symbol):
    """Tageskerzen der letzten 5 Jahre, zwischengespeichert in daten/ (einmal pro Tag neu)."""
    datei = ORDNER / "daten" / f"{symbol.replace('/', '-')}_1Day.csv"
    if datei.exists() and dt.date.fromtimestamp(datei.stat().st_mtime) == dt.date.today():
        return pd.read_csv(datei, index_col=0, parse_dates=True)

    from alpaca.data.timeframe import TimeFrame
    ende = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    start = ende - dt.timedelta(days=JAHRE * 365 + 2)
    if "/" in symbol:
        from alpaca.data.historical import CryptoHistoricalDataClient
        from alpaca.data.requests import CryptoBarsRequest
        bars = CryptoHistoricalDataClient().get_crypto_bars(
            CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=ende))
    else:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        load_dotenv(ORDNER / ".env")
        key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise SystemExit("Alpaca-Schlüssel fehlen in .env (ALPACA_API_KEY, ALPACA_SECRET_KEY).")
        bars = StockHistoricalDataClient(key, secret).get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=ende,
            feed=DataFeed.IEX, adjustment=Adjustment.ALL))

    df = bars.df
    if df.empty:
        raise SystemExit(f"Keine Kurse für {symbol} gefunden. Stimmt das Symbol?")
    df = df.xs(symbol, level="symbol") if "symbol" in df.index.names else df
    df = df.rename(columns=str.capitalize)[["Open", "High", "Low", "Close", "Volume"]]
    df.index = pd.DatetimeIndex(df.index).tz_localize(None)
    datei.parent.mkdir(exist_ok=True)
    df.to_csv(datei)
    return df


def urteil(stats):
    gruende = []
    if stats["Return [%]"] <= 0:
        gruende.append("Rendite 0 % oder weniger")
    if abs(stats["Max. Drawdown [%]"]) > risiko.MAX_DRAWDOWN_PROZENT:
        gruende.append(f"Drawdown über {risiko.MAX_DRAWDOWN_PROZENT} %")
    if stats["# Trades"] < MIN_TRADES:
        gruende.append(f"weniger als {MIN_TRADES} Trades")
    return ("ROT (" + ", ".join(gruende) + ")") if gruende else "GRÜN"


def zahl(wert):
    wert = float(wert)
    return round(wert, 2) if math.isfinite(wert) else None


def bericht_json(pfad, modul, kurse, stats, ergebnis):
    """Maschinenlesbarer Bericht für den Trading-Bot: Er handelt nur grüne, unveränderte Versionen."""
    return {"strategie": pfad.stem, "symbol": modul.SYMBOL,
            "von": f"{kurse.index[0]:%Y-%m-%d}", "bis": f"{kurse.index[-1]:%Y-%m-%d}",
            "rendite": zahl(stats["Return [%]"]), "kaufen_halten": zahl(stats["Buy & Hold Return [%]"]),
            "max_drawdown": zahl(stats["Max. Drawdown [%]"]), "trades": int(stats["# Trades"]),
            "gewinnquote": zahl(stats["Win Rate [%]"]),
            "ergebnis": "gruen" if ergebnis == "GRÜN" else "rot", "urteil": ergebnis,
            "risiko": {"stop_loss": risiko.STOP_LOSS_PROZENT, "max_drawdown": risiko.MAX_DRAWDOWN_PROZENT},
            "sha256": hashlib.sha256(pfad.read_bytes()).hexdigest(),
            "getestet_am": dt.datetime.now().isoformat(timespec="minutes")}


def kennzahlen(stats):
    return {"rendite": zahl(stats["Return [%]"]), "kaufen_halten": zahl(stats["Buy & Hold Return [%]"]),
            "max_drawdown": zahl(stats["Max. Drawdown [%]"]), "trades": int(stats["# Trades"]),
            "gewinnquote": zahl(stats["Win Rate [%]"])}


def listen_test(pfad, modul, berichte):
    """SYMBOL = "LISTE": jede Aktie der Bot-Liste einzeln testen. Grün, wenn mindestens die Hälfte grün ist."""
    import liste
    symbole, eigene, _ = liste.symbole()
    ordner = berichte / pfad.stem
    ordner.mkdir(exist_ok=True)
    aktien, alle_trades = {}, []
    print(f"{pfad.stem} · Liste mit {len(symbole)} Aktien")
    for symbol in symbole:
        try:
            kurse = lade_kurse(symbol)
            bt = Backtest(kurse, modul.Strategie, cash=STARTKAPITAL, commission=KOSTEN, finalize_trades=True)
            stats = bt.run()
        except (SystemExit, Exception) as fehler:
            print(f"  {symbol:6} übersprungen: {fehler}")
            continue
        ergebnis = urteil(stats)
        (ordner / f"{symbol}.txt").write_text(str(stats), encoding="utf-8")
        try:
            bt.plot(filename=str(ordner / f"{symbol}.html"), open_browser=False)
        except Exception:
            pass  # Chart ist nur Beiwerk
        trades = stats["_trades"].copy()
        trades.insert(0, "Symbol", symbol)
        alle_trades.append(trades)
        aktien[symbol] = {**kennzahlen(stats), "ergebnis": "gruen" if ergebnis == "GRÜN" else "rot", "urteil": ergebnis,
                          "eigene": symbol in eigene, "von": f"{kurse.index[0]:%Y-%m-%d}", "bis": f"{kurse.index[-1]:%Y-%m-%d}"}
        print(f"  {symbol:6} Rendite {stats['Return [%]']:6.1f} % · Kaufen-und-Halten {stats['Buy & Hold Return [%]']:7.1f} % · "
              f"max. Drawdown {stats['Max. Drawdown [%]']:6.1f} % · Trades {stats['# Trades']:3} · {ergebnis}")
    if not aktien:
        raise SystemExit("Keine Aktie der Liste ließ sich testen.")
    gruen = sum(1 for a in aktien.values() if a["ergebnis"] == "gruen")
    ergebnis = (f"GRÜN ({gruen} von {len(aktien)} Aktien grün)" if gruen * 2 >= len(aktien)
                else f"ROT (nur {gruen} von {len(aktien)} Aktien grün, nötig ist mindestens die Hälfte)")
    pd.concat(alle_trades).to_csv(berichte / f"{pfad.stem}_trades.csv", index=False)
    werte = list(aktien.values())
    mittel = lambda key: zahl(sum(a[key] or 0 for a in werte) / len(werte))
    bericht = {"strategie": pfad.stem, "symbol": "LISTE", "anzahl": len(aktien), "gruen_anzahl": gruen,
               "rendite": mittel("rendite"), "kaufen_halten": mittel("kaufen_halten"),
               "max_drawdown": min(a["max_drawdown"] or 0 for a in werte), "trades": sum(a["trades"] for a in werte),
               "ergebnis": "gruen" if ergebnis.startswith("GRÜN") else "rot", "urteil": ergebnis, "aktien": aktien,
               "risiko": {"stop_loss": risiko.STOP_LOSS_PROZENT, "max_drawdown": risiko.MAX_DRAWDOWN_PROZENT},
               "sha256": hashlib.sha256(pfad.read_bytes()).hexdigest(),
               "getestet_am": dt.datetime.now().isoformat(timespec="minutes")}
    (berichte / f"{pfad.stem}.json").write_text(json.dumps(bericht, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Durchschnitt: Rendite {bericht['rendite']} % · Kaufen-und-Halten {bericht['kaufen_halten']} % · "
          f"schlechtester Drawdown {bericht['max_drawdown']} % · Trades gesamt {bericht['trades']}")
    print(f"Trades: berichte/{pfad.stem}_trades.csv (Spalte Symbol) · Charts: berichte/{pfad.stem}/")
    print(f"ERGEBNIS: {ergebnis}")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Aufruf: python stresstest.py strategien/<name>_v1.py")
    risiko.pruefe()
    pfad = Path(sys.argv[1]).resolve()
    modul = lade_strategie(pfad)
    berichte = ORDNER / "berichte"
    berichte.mkdir(exist_ok=True)
    warnings.filterwarnings("ignore")
    if modul.SYMBOL == "LISTE":
        return listen_test(pfad, modul, berichte)
    kurse = lade_kurse(modul.SYMBOL)

    bt = Backtest(kurse, modul.Strategie, cash=STARTKAPITAL, commission=KOSTEN, finalize_trades=True)
    stats = bt.run()

    (berichte / f"{pfad.stem}.txt").write_text(str(stats), encoding="utf-8")
    stats["_trades"].to_csv(berichte / f"{pfad.stem}_trades.csv", index=False)
    try:
        bt.plot(filename=str(berichte / f"{pfad.stem}.html"), open_browser=False)
    except Exception as fehler:  # Chart ist nur Beiwerk
        print(f"Chart nicht erstellt: {fehler}")

    print(f"{pfad.stem} · {modul.SYMBOL} · {kurse.index[0]:%d.%m.%Y} bis {kurse.index[-1]:%d.%m.%Y}")
    print(f"Rendite {stats['Return [%]']:.1f} % · Kaufen-und-Halten {stats['Buy & Hold Return [%]']:.1f} % · "
          f"max. Drawdown {stats['Max. Drawdown [%]']:.1f} % · Trades {stats['# Trades']} · "
          f"Gewinnquote {stats['Win Rate [%]']:.0f} %")
    ergebnis = urteil(stats)
    (berichte / f"{pfad.stem}.json").write_text(
        json.dumps(bericht_json(pfad, modul, kurse, stats, ergebnis), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Trades: berichte/{pfad.stem}_trades.csv · Chart: berichte/{pfad.stem}.html")
    print(f"ERGEBNIS: {ergebnis}")


if __name__ == "__main__":
    main()
