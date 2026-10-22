"""Five-year stress tests of stocks/ETFs with a verdict per stock.

SYMBOL = "LISTE" tests the bots' list plus external holdings (see liste.py).
List summaries are informational; only each stock's own verdict gates its entries.

Self-tuning: a strategy may declare PARAMETER = {"NAME": [values, ...]} for its module constants.
Each stock then gets its own values by walk-forward: for each of the last TEST_JAHRE years the
best values are chosen on the years before it and judged on that unseen year. Only this
out-of-sample result decides green or red. The values for live signals are chosen on all
five years. Strategy files are never changed; the chosen values live in the report.

Intraday: a strategy with ZEITRAHMEN = "5Min" (or 15Min, 30Min, 1Hour) is tested on about
60 sessions of regular-session bars. Positions are closed before every close (never held
overnight), and the walk-forward judges the last TEST_SITZUNGEN sessions in two parts.
"""
from contextlib import contextmanager
import datetime as dt
import hashlib
import importlib.util
import itertools
import json
import math
import os
import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from backtesting import Backtest
from dotenv import load_dotenv

import risiko

ORDNER = Path(__file__).resolve().parent
JAHRE = 5
STARTKAPITAL = 100_000
KOSTEN = 0.001          # 0,1 % pro Order als Puffer für Spread und Gebühren
MIN_TRADES = 10
# Walk-forward: die letzten TEST_JAHRE Jahre werden je mit Werten gehandelt, die nur ältere Kurse kannten.
TEST_JAHRE = 2
MIN_TRADES_TEST = math.ceil(MIN_TRADES * TEST_JAHRE / JAHRE)
MIN_LERN_TAGE = 400        # mindestens gut anderthalb Jahre zum Anpassen
MAX_KOMBINATIONEN = 36     # so viele Wertekombinationen höchstens pro Anpassung
MAX_WERTE = 10             # so viele Werte höchstens pro Parameter
DRAWDOWN_GEWICHT = .5      # Anpassung: Rendite minus halber Drawdown, damit wilde Kurven nicht gewinnen
# Intraday (ZEITRAHMEN unter einem Tag)
ZEITRAHMEN = {"1Day": None, "5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60}
INTRADAY_KALENDERTAGE = 90     # rund 60 Handelstage Kerzen
TEST_SITZUNGEN = 20            # die letzten 20 Handelstage entscheiden, in zwei Hälften angepasst
MIN_LERN_SITZUNGEN = 20
MIN_TRADES_INTRADAY = 10       # mindestens so viele Trades in den TEST_SITZUNGEN
SCHLUSS_MINUTEN = 10           # so lange vor Börsenschluss ist jede Position geschlossen, keine neuen Käufe


def dateiname(symbol):
    return symbol.replace("/", "-")


def lade_strategie(pfad):
    spec = importlib.util.spec_from_file_location(pfad.stem, pfad)
    modul = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ORDNER))
    spec.loader.exec_module(modul)
    if not hasattr(modul, "SYMBOL") or not hasattr(modul, "Strategie"):
        raise SystemExit(f"{pfad.name} braucht SYMBOL und die Klasse Strategie.")
    return modul


def zeitrahmen(modul):
    wert = getattr(modul, "ZEITRAHMEN", "1Day")
    if wert not in ZEITRAHMEN:
        raise SystemExit(f"ZEITRAHMEN muss einer von {', '.join(ZEITRAHMEN)} sein (jetzt: {wert!r}).")
    return wert


def ist_intraday(kurse):
    """Intraday-Kerzen haben Uhrzeiten; Tageskerzen liegen alle auf Mitternacht."""
    return bool(len(kurse)) and bool((kurse.index != kurse.index.normalize()).any())


def lade_kurse(symbol, rahmen="1Day"):
    """Tageskerzen der letzten 5 Jahre (einmal pro Tag neu) oder rund 60 Handelstage Intraday-Kerzen
    der regulären Handelszeit in New Yorker Zeit (alle 30 Minuten neu), zwischengespeichert in daten/."""
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
        raise ValueError("Nur US-Aktien und ETFs werden unterstützt")
    minuten = ZEITRAHMEN[rahmen]
    datei = ORDNER / "daten" / f"{symbol}_{rahmen}.csv"
    if datei.exists() and (dt.datetime.now().timestamp() - datei.stat().st_mtime < 1800 if minuten
                           else dt.date.fromtimestamp(datei.stat().st_mtime) == dt.date.today()):
        return pd.read_csv(datei, index_col=0, parse_dates=True)

    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    if minuten:
        ende = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=16)  # kostenlose Daten: 15 Minuten Verzug
        start = ende - dt.timedelta(days=INTRADAY_KALENDERTAGE)
        rahmen_alpaca = TimeFrame(minuten // 60, TimeFrameUnit.Hour) if minuten >= 60 else TimeFrame(minuten, TimeFrameUnit.Minute)
    else:
        ende = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        start = ende - dt.timedelta(days=JAHRE * 365 + 2)
        rahmen_alpaca = TimeFrame.Day
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    load_dotenv(ORDNER / ".env")
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit("Alpaca-Schlüssel fehlen in .env (ALPACA_API_KEY, ALPACA_SECRET_KEY).")
    bars = StockHistoricalDataClient(key, secret).get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=rahmen_alpaca, start=start, end=ende,
        feed=DataFeed.IEX, adjustment=Adjustment.ALL))

    df = bars.df
    if df.empty:
        raise SystemExit(f"Keine Kurse für {symbol} gefunden. Stimmt das Symbol?")
    df = df.xs(symbol, level="symbol") if "symbol" in df.index.names else df
    df = df.rename(columns=str.capitalize)[["Open", "High", "Low", "Close", "Volume"]]
    if minuten:
        # Nur abgeschlossene Kerzen der regulären Handelszeit, in New Yorker Uhrzeit.
        lokal = pd.DatetimeIndex(df.index).tz_convert("America/New_York")
        beginn = lokal.hour * 60 + lokal.minute
        behalten = (beginn >= 9 * 60 + 30) & (beginn + minuten <= 16 * 60)
        df = df[behalten]
        df.index = lokal[behalten].tz_localize(None)
        if df.empty:
            raise SystemExit(f"Keine Intraday-Kerzen für {symbol} in der regulären Handelszeit.")
    else:
        df.index = pd.DatetimeIndex(df.index).tz_localize(None)
    datei.parent.mkdir(exist_ok=True)
    df.to_csv(datei)
    return df


def urteil(stats):
    return urteil_werte(stats["Return [%]"], stats["Max. Drawdown [%]"], stats["# Trades"])


def urteil_werte(rendite, drawdown, trades, min_trades=MIN_TRADES):
    if not all(math.isfinite(float(v)) for v in (rendite, drawdown, trades)):
        return "ROT (ungültige Kennzahlen)"
    gruende = []
    if rendite <= 0:
        gruende.append("Rendite 0 % oder weniger")
    if abs(drawdown) > risiko.MAX_DRAWDOWN_PROZENT:
        gruende.append(f"Drawdown über {risiko.MAX_DRAWDOWN_PROZENT} %")
    if trades < min_trades:
        gruende.append(f"weniger als {min_trades} Trades")
    return ("ROT (" + ", ".join(gruende) + ")") if gruende else "GRÜN"


# Selbst-Anpassung -------------------------------------------------------------

def parameter_raster(modul):
    """(Standardwerte, Kombinationen) aus PARAMETER der Strategie; ohne PARAMETER nur die Standardwerte."""
    raster = getattr(modul, "PARAMETER", None) or {}
    if not isinstance(raster, dict):
        raise SystemExit("PARAMETER muss ein dict {NAME: [Werte]} sein.")
    for name, werte in raster.items():
        if not (isinstance(name, str) and name.isupper() and name != "SYMBOL" and hasattr(modul, name)):
            raise SystemExit(f"PARAMETER: {name!r} ist keine Konstante der Strategie.")
        if (not isinstance(werte, (list, tuple)) or not 1 <= len(werte) <= MAX_WERTE
                or any(isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) for w in werte)):
            raise SystemExit(f"PARAMETER {name}: 1 bis {MAX_WERTE} Zahlen angeben.")
    standard = {name: getattr(modul, name) for name in raster}
    kombinationen = [dict(zip(raster, werte)) for werte in itertools.product(*raster.values())]
    if len(kombinationen) > MAX_KOMBINATIONEN:
        # Gleichmäßig verteilte Auswahl, immer dieselbe.
        schritt = (len(kombinationen) - 1) / (MAX_KOMBINATIONEN - 1)
        kombinationen = [kombinationen[round(i * schritt)] for i in range(MAX_KOMBINATIONEN)]
    if standard not in kombinationen:
        kombinationen.insert(0, standard)
    return standard, kombinationen


@contextmanager
def parameter(modul, werte):
    """Setzt Konstanten der Strategie nur für die Dauer eines Backtests; die Datei bleibt unverändert."""
    alt = {name: getattr(modul, name) for name in werte}
    try:
        for name, wert in werte.items():
            setattr(modul, name, wert)
        yield
    finally:
        for name, wert in alt.items():
            setattr(modul, name, wert)


def schluss_kerzen(index):
    """True für Kerzen, die höchstens SCHLUSS_MINUTEN vor dem Ende ihrer Sitzung enden.

    Vergangene Sitzungen enden mit ihrer letzten Kerze (Halbtage inklusive). Die letzte Sitzung kann noch
    laufen: Sie endet frühestens um 16:00, damit die neueste Kerze nicht als Schluss gilt (den Halbtag
    heute erkennt der Bot an der Börsenuhr).
    """
    if len(index) < 2:
        return np.zeros(len(index), dtype=bool)
    abstaende = pd.Series(index).diff().dropna()
    schritt = abstaende[abstaende > pd.Timedelta(0)].min()
    tage = index.normalize()
    ende = pd.Series(index + schritt)
    sitzung = ende.groupby(tage.values).transform("max")
    letzte = tage == tage[-1]
    sitzung[letzte] = sitzung[letzte].clip(lower=tage[-1] + pd.Timedelta(hours=16))
    return ((sitzung - ende) <= pd.Timedelta(minutes=SCHLUSS_MINUTEN)).to_numpy()


def strategie_klasse(modul, kurse):
    """Tageskerzen: die Strategie unverändert. Intraday: dieselbe Strategie, aber vor jedem Schluss flach,
    wie der Bot (keine Position über Nacht, keine Käufe in den letzten SCHLUSS_MINUTEN)."""
    basis = modul.Strategie
    if not ist_intraday(kurse):
        return basis
    schluss = schluss_kerzen(kurse.index)

    class Tagesende(basis):
        def next(self):
            if schluss[len(self.data) - 1]:
                for order in list(self.orders):
                    if not order.is_contingent:
                        order.cancel()
                if self.position:
                    self.position.close()
                return
            super().next()

    Tagesende.__name__ = basis.__name__
    return Tagesende


def backtest(modul, kurse, werte, finalize=True):
    with parameter(modul, werte):
        bt = Backtest(kurse, strategie_klasse(modul, kurse), cash=STARTKAPITAL, commission=KOSTEN, finalize_trades=finalize)
        return bt, bt.run()


def bewertung(stats, min_trades):
    rendite, drawdown, trades = (float(stats[k]) for k in ("Return [%]", "Max. Drawdown [%]", "# Trades"))
    if not all(math.isfinite(v) for v in (rendite, drawdown)) or trades < min_trades:
        return None
    if abs(drawdown) > risiko.MAX_DRAWDOWN_PROZENT:
        return None
    return rendite - DRAWDOWN_GEWICHT * abs(drawdown)


def beste_werte(modul, kurse, kombinationen, standard):
    """Die Kombination mit der besten Bewertung auf diesen Kursen; ohne gültige bleiben die Standardwerte."""
    if len(kombinationen) < 2:
        return standard
    if ist_intraday(kurse):
        min_trades = max(1, round(MIN_TRADES_INTRADAY * kurse.index.normalize().nunique() / TEST_SITZUNGEN))
    else:
        min_trades = max(1, round(MIN_TRADES * len(kurse) / (JAHRE * 252)))
    bester = None
    for werte in kombinationen:
        try:
            _, stats = backtest(modul, kurse, werte)
        except Exception:
            continue  # Eine unbrauchbare Kombination darf die anderen nicht verhindern.
        punkte = bewertung(stats, min_trades)
        if punkte is not None and (bester is None or punkte > bester[0]):
            bester = (punkte, werte)
    return bester[1] if bester else standard


def fenster_kurve(stats, start):
    """Kontowert ab start, normiert auf den Stand direkt davor."""
    kapital = stats["_equity_curve"]["Equity"]
    vorher = kapital[kapital.index < start]
    basis = float(vorher.iloc[-1]) if len(vorher) else float(STARTKAPITAL)
    return kapital[kapital.index >= start] / basis


def test_grenzen(kurse):
    """Beginn jedes Testabschnitts: Tageskerzen je ein Jahr, Intraday je die Hälfte der TEST_SITZUNGEN."""
    if not ist_intraday(kurse):
        ende = kurse.index[-1]
        return [ende - pd.DateOffset(years=TEST_JAHRE - i) for i in range(TEST_JAHRE)]
    tage = kurse.index.normalize().unique()
    if len(tage) < TEST_SITZUNGEN + MIN_LERN_SITZUNGEN:
        raise ValueError(f"zu wenig Intraday-Kurshistorie (mindestens {TEST_SITZUNGEN + MIN_LERN_SITZUNGEN} Handelstage)")
    return [tage[-TEST_SITZUNGEN], tage[-TEST_SITZUNGEN // 2]]


def walk_forward(modul, kurse, kombinationen, standard):
    """Out-of-sample-Kennzahlen der Testabschnitte, jeder mit Werten, die nur ältere Kurse kannten."""
    grenzen = test_grenzen(kurse)
    intraday = ist_intraday(kurse)
    kurven, trades, jahre, stand = [], 0, [], 1.
    for i, start in enumerate(grenzen):
        stop = grenzen[i + 1] if i + 1 < len(grenzen) else None
        lernen = kurse[kurse.index < start]
        if not intraday and len(lernen) < MIN_LERN_TAGE:
            raise ValueError(f"zu wenig Kurshistorie für den Walk-forward (mindestens {MIN_LERN_TAGE} Tage vor dem Test)")
        werte = beste_werte(modul, lernen, kombinationen, standard)
        teil = kurse if stop is None else kurse[kurse.index < stop]
        _, stats = backtest(modul, teil, werte)
        kurve = fenster_kurve(stats, start)
        if kurve.empty:
            continue
        kurven.append(kurve * stand)
        stand = float(kurven[-1].iloc[-1])
        geschlossen = stats["_trades"]
        trades += int((pd.to_datetime(geschlossen["ExitTime"]) >= start).sum()) if len(geschlossen) else 0
        jahre.append({"von": f"{kurve.index[0]:%Y-%m-%d}", "bis": f"{kurve.index[-1]:%Y-%m-%d}", "parameter": werte,
                      "rendite": zahl((float(kurve.iloc[-1]) - 1) * 100)})
    if not kurven:
        raise ValueError("kein Testzeitraum mit Kursen")
    kurve = pd.concat(kurven)
    spitze = kurve.cummax().clip(lower=1.)
    vorher = kurse.Close[kurse.index < grenzen[0]]
    basis = float(vorher.iloc[-1]) if len(vorher) else float(kurse.Close.iloc[0])
    return {"rendite": zahl((stand - 1) * 100), "max_drawdown": zahl(float((kurve / spitze - 1).min()) * 100),
            "trades": trades, "kaufen_halten": zahl((float(kurse.Close.iloc[-1]) / basis - 1) * 100),
            "von": f"{kurve.index[0]:%Y-%m-%d}", "bis": f"{kurve.index[-1]:%Y-%m-%d}", "jahre": jahre}


def pruefe(modul, kurse):
    """Einzeltest einer Aktie: (Bericht-Eintrag, Backtest, Statistik) mit den Werten für Live-Signale.

    Mit PARAMETER entscheidet der Walk-forward der letzten TEST_JAHRE Jahre, sonst wie bisher die 5 Jahre.
    """
    standard, kombinationen = parameter_raster(modul)
    intraday = ist_intraday(kurse)
    werte = beste_werte(modul, kurse, kombinationen, standard)
    bt, stats = backtest(modul, kurse, werte)
    gesamt = kennzahlen(stats)
    # Intraday entscheidet immer der Testabschnitt (gut 60 Sitzungen sind für ein Gesamturteil zu wenig).
    walk = len(kombinationen) > 1 or intraday
    eintrag = {"methode": "walk_forward" if walk else "fuenf_jahre", "zeitrahmen": zeitrahmen(modul),
               "parameter": werte, "standard": standard, "angepasst": werte != standard,
               "von": f"{kurse.index[0]:%Y-%m-%d}", "bis": f"{kurse.index[-1]:%Y-%m-%d}"}
    if walk:
        test = walk_forward(modul, kurse, kombinationen, standard)
        ergebnis = urteil_werte(test["rendite"], test["max_drawdown"], test["trades"],
                                MIN_TRADES_INTRADAY if intraday else MIN_TRADES_TEST)
        eintrag.update({k: test[k] for k in ("rendite", "kaufen_halten", "max_drawdown", "trades")},
                       gewinnquote=gesamt["gewinnquote"], test=test, gesamt=gesamt)
    else:
        ergebnis = urteil(stats)
        eintrag.update(gesamt)
    eintrag.update(ergebnis="gruen" if ergebnis == "GRÜN" else "rot", urteil=ergebnis)
    return eintrag, bt, stats


def werte_text(werte):
    return ", ".join(f"{k}={v}" for k, v in werte.items()) or "keine"


def zahl(wert):
    wert = float(wert)
    return round(wert, 2) if math.isfinite(wert) else None


def bericht_json(pfad, modul, eintrag):
    """Maschinenlesbarer Einzeltest für den Trading-Bot."""
    return {"strategie": pfad.stem, "symbol": modul.SYMBOL, **eintrag,
            "risiko": {"stop_loss": risiko.STOP_LOSS_PROZENT, "max_drawdown": risiko.MAX_DRAWDOWN_PROZENT},
            "sha256": hashlib.sha256(pfad.read_bytes()).hexdigest(),
            "getestet_am": dt.datetime.now().isoformat(timespec="minutes")}


def kennzahlen(stats):
    return {"rendite": zahl(stats["Return [%]"]), "kaufen_halten": zahl(stats["Buy & Hold Return [%]"]),
            "max_drawdown": zahl(stats["Max. Drawdown [%]"]), "trades": int(stats["# Trades"]),
            "gewinnquote": zahl(stats["Win Rate [%]"])}


def zeile(eintrag):
    """Eine Ergebniszeile; bei angepassten Strategien zählt der Testzeitraum, den die Anpassung nicht kannte."""
    zeitraum = (f"Test {eintrag['test']['von']} bis {eintrag['test']['bis']} · " if eintrag["methode"] == "walk_forward" else "")
    werte = f" · Werte {werte_text(eintrag['parameter'])}" if eintrag["parameter"] else ""
    zahl_text = lambda key, breite: "—" if eintrag[key] is None else f"{eintrag[key]:{breite}.1f}"
    return (f"{zeitraum}Rendite {zahl_text('rendite', 6)} % · Kaufen-und-Halten {zahl_text('kaufen_halten', 7)} % · "
            f"max. Drawdown {zahl_text('max_drawdown', 6)} % · Trades {eintrag['trades']:3}{werte} · {eintrag['urteil']}")


def listen_urteil(gruen, anzahl):
    return f"EINZELTESTS ({gruen} von {anzahl} Aktien grün)"


def listen_test(pfad, modul, berichte):
    """Jede Aktie einzeln testen; die Liste hat kein gemeinsames Kaufurteil."""
    import liste
    symbole, eigene, _ = liste.symbole()
    wort = "Aktien"
    ordner = berichte / pfad.stem
    ordner.mkdir(exist_ok=True)
    aktien, alle_trades = {}, []
    print(f"{pfad.stem} · Liste mit {len(symbole)} {wort}")
    for symbol in symbole:
        try:
            kurse = lade_kurse(symbol, zeitrahmen(modul))
            eintrag, bt, stats = pruefe(modul, kurse)
        except (SystemExit, Exception) as fehler:
            print(f"  {symbol:6} übersprungen: {fehler}")
            aktien[symbol] = {"ergebnis": "fehler", "urteil": str(fehler)[:200], "eigene": symbol in eigene}
            continue
        ergebnis = eintrag["urteil"]
        (ordner / f"{dateiname(symbol)}.txt").write_text(str(stats), encoding="utf-8")
        try:
            bt.plot(filename=str(ordner / f"{dateiname(symbol)}.html"), open_browser=False)
        except Exception:
            pass  # Chart ist nur Beiwerk
        trades = stats["_trades"].copy()
        trades.insert(0, "Symbol", symbol)
        alle_trades.append(trades)
        aktien[symbol] = {**eintrag, "eigene": symbol in eigene}
        print(f"  {symbol:6} {zeile(eintrag)}")
    if not alle_trades:
        raise SystemExit(f"Kein Wert der Liste ließ sich testen ({wort}).")
    gruen = sum(1 for a in aktien.values() if a["ergebnis"] == "gruen")
    ergebnis = listen_urteil(gruen, len(aktien))
    pd.concat(alle_trades).to_csv(berichte / f"{pfad.stem}_trades.csv", index=False)
    werte = [a for a in aktien.values() if a["ergebnis"] != "fehler"]
    mittel = lambda key: zahl(sum(a[key] or 0 for a in werte) / len(werte))
    bericht = {"strategie": pfad.stem, "symbol": modul.SYMBOL, "anzahl": len(aktien), "gruen_anzahl": gruen,
               "rendite": mittel("rendite"), "kaufen_halten": mittel("kaufen_halten"),
               "max_drawdown": min(a["max_drawdown"] or 0 for a in werte), "trades": sum(a["trades"] for a in werte),
               "ergebnis": "einzeltests", "urteil": ergebnis, "aktien": aktien,
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
    kurse = lade_kurse(modul.SYMBOL, zeitrahmen(modul))
    eintrag, bt, stats = pruefe(modul, kurse)

    (berichte / f"{pfad.stem}.txt").write_text(str(stats), encoding="utf-8")
    stats["_trades"].to_csv(berichte / f"{pfad.stem}_trades.csv", index=False)
    try:
        bt.plot(filename=str(berichte / f"{pfad.stem}.html"), open_browser=False)
    except Exception as fehler:  # Chart ist nur Beiwerk
        print(f"Chart nicht erstellt: {fehler}")

    print(f"{pfad.stem} · {modul.SYMBOL} · {kurse.index[0]:%d.%m.%Y} bis {kurse.index[-1]:%d.%m.%Y}")
    print(zeile(eintrag))
    (berichte / f"{pfad.stem}.json").write_text(
        json.dumps(bericht_json(pfad, modul, eintrag), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Trades: berichte/{pfad.stem}_trades.csv · Chart: berichte/{pfad.stem}.html")
    print(f"ERGEBNIS: {eintrag['urteil']}")


if __name__ == "__main__":
    main()
