"""Qualität eines Einzeltests, 0 bis 100 Punkte.

Nach der Bewertung des Skills "backtest-expert" (claude-trading-skills, MIT-Lizenz, TraderMonty):
fünf Bereiche zu je 20 Punkten. Stichprobe (Anzahl Trades), Erwartungswert pro Trade,
Risiko (Drawdown und Profitfaktor), Robustheit (Testjahre, Anzahl angepasster Parameter) und
Ausführung (bleibt die Strategie mit doppelten Kosten im Plus?).

Die Punkte ändern nichts an grün oder rot (das entscheidet weiter `urteil()` im Stresstest).
Der Bot nutzt sie nur, um die Positionsgröße einer Aktie anzupassen.
"""
import math


def trade_kennzahlen(trades):
    """Gewinnquote, durchschnittlicher Gewinn und Verlust in % aus der Trade-Tabelle von backtesting.py."""
    werte = [float(r) * 100 for r in getattr(trades, "ReturnPct", []) if math.isfinite(float(r))]
    gewinne = [w for w in werte if w > 0]
    verluste = [-w for w in werte if w <= 0]
    if not werte:
        return {"anzahl": 0, "gewinnquote": None, "avg_gewinn": None, "avg_verlust": None,
                "profitfaktor": None, "erwartung": None}
    quote = len(gewinne) / len(werte) * 100
    avg_gewinn = sum(gewinne) / len(gewinne) if gewinne else 0.
    avg_verlust = sum(verluste) / len(verluste) if verluste else 0.
    summe_verlust = sum(verluste)
    faktor = (sum(gewinne) / summe_verlust) if summe_verlust > 0 else (None if not gewinne else 99.)
    erwartung = quote / 100 * avg_gewinn - (1 - quote / 100) * avg_verlust
    runden = lambda v: None if v is None else round(v, 2)
    return {"anzahl": len(werte), "gewinnquote": runden(quote), "avg_gewinn": runden(avg_gewinn),
            "avg_verlust": runden(avg_verlust), "profitfaktor": runden(faktor), "erwartung": runden(erwartung)}


def punkte_stichprobe(trades):
    if trades < 30:
        # Unter 30 Trades sagt die Statistik wenig: höchstens 6 Punkte, damit 10–29 Trades nicht gleich null sind.
        return min(6, trades // 5)
    if trades < 100:
        return 8 + int((trades - 30) / 70 * 7)
    if trades < 200:
        return 15 + int((trades - 100) / 100 * 5)
    return 20


def punkte_erwartung(erwartung):
    if erwartung is None or erwartung <= 0:
        return 0
    if erwartung < .5:
        return 5 + int(erwartung / .5 * 5)
    if erwartung < 1.5:
        return 10 + int((erwartung - .5) * 8)
    return 20


def punkte_risiko(max_drawdown, profitfaktor):
    drawdown = abs(max_drawdown or 0)
    if drawdown >= 50:
        return 0
    dd = 12 if drawdown < 20 else int(12 * (50 - drawdown) / 30)
    if profitfaktor is None or profitfaktor <= 1:
        pf = 0
    elif profitfaktor >= 3:
        pf = 8
    else:
        pf = int((profitfaktor - 1) / 2 * 8)
    return min(20, dd + pf)


def punkte_robustheit(jahre, parameter):
    jahre_punkte = 0 if jahre < 5 else 15 if jahre >= 10 else 5 + int((jahre - 5) / 5 * 10)
    parameter_punkte = 5 if parameter <= 4 else 3 if parameter <= 6 else 1 if parameter == 7 else 0
    return min(20, jahre_punkte + parameter_punkte)


def bewerten(kennzahlen, max_drawdown, jahre, parameter, rendite_doppelte_kosten):
    """{"punkte": 0..100, "bereiche": {...}, "stufe": "stark"/"mittel"/"schwach"} für einen Einzeltest."""
    bereiche = {
        "stichprobe": punkte_stichprobe(kennzahlen.get("anzahl") or 0),
        "erwartung": punkte_erwartung(kennzahlen.get("erwartung")),
        "risiko": punkte_risiko(max_drawdown, kennzahlen.get("profitfaktor")),
        "robustheit": punkte_robustheit(jahre, parameter),
        "ausfuehrung": 20 if rendite_doppelte_kosten is not None and rendite_doppelte_kosten > 0 else 0,
    }
    punkte = sum(bereiche.values())
    stufe = "stark" if punkte >= 70 else "mittel" if punkte >= 40 else "schwach"
    return {"punkte": punkte, "bereiche": bereiche, "stufe": stufe}
