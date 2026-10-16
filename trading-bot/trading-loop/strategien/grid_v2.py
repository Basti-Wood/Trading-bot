"""grid v2: Gitter nur im Seitwärtsmarkt. Die Spanne zwischen Hoch und Tief der letzten 20 Tage wird in 10 Linien
geteilt. Fällt der Kurs auf eine Linie und zeigt der ADX keinen Trend, kauft die Strategie ein Los; an der nächsten
Linie darüber verkauft sie es wieder."""
import pandas as pd
from backtesting import Strategy
import risiko

SYMBOL = "LISTE"   # Aktienliste der Bots plus eigene Aktien

SPANNE_TAGE = 20   # Spanne wandert mit: Hoch und Tief der vorherigen 20 Tage
LINIEN = 10
LOS = 0.95 / LINIEN   # Anteil des Kapitals pro Linie, alle Linien zusammen höchstens 95 %
ADX_TAGE = 14
ADX_TREND = 20     # üblicher Schwellenwert laut Quellen: unter 20 gilt als trendlos (seitwärts)


def hoch(werte, tage):
    return pd.Series(werte).rolling(tage).max().shift(1).values


def tief(werte, tage):
    return pd.Series(werte).rolling(tage).min().shift(1).values


def adx(hoch, tief, schluss, tage):
    """Average Directional Index nach Wilder: Stärke des Trends, egal in welche Richtung."""
    hoch, tief, schluss = pd.Series(hoch), pd.Series(tief), pd.Series(schluss)
    auf, ab = hoch.diff(), -tief.diff()
    plus_dm = auf.where((auf > ab) & (auf > 0), 0.)
    minus_dm = ab.where((ab > auf) & (ab > 0), 0.)
    tr = pd.concat([hoch - tief, (hoch - schluss.shift()).abs(), (tief - schluss.shift()).abs()], axis=1).max(axis=1)
    glatt = lambda reihe: reihe.ewm(alpha=1 / tage, adjust=False, min_periods=tage).mean()
    atr = glatt(tr)
    plus_di, minus_di = 100 * glatt(plus_dm) / atr, 100 * glatt(minus_dm) / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return glatt(dx).values


class Strategie(Strategy):
    def init(self):
        self.oben = self.I(hoch, self.data.High, SPANNE_TAGE, name="Gitter oben")
        self.unten = self.I(tief, self.data.Low, SPANNE_TAGE, name="Gitter unten")
        self.adx = self.I(adx, self.data.High, self.data.Low, self.data.Close, ADX_TAGE, name=f"ADX {ADX_TAGE}", overlay=False)

    def next(self):
        if len(self.data) < SPANNE_TAGE + 2:
            return
        oben, unten = self.oben[-1], self.unten[-1]
        abstand = (oben - unten) / LINIEN
        if not abstand > 0:
            return
        vorher, kurs = self.data.Close[-2], self.data.Close[-1]
        # EINSTIEG: für jede Gitterlinie, die der Kurs heute von oben nach unten erreicht hat, ein Los kaufen,
        # aber nur, solange der ADX unter 20 liegt.
        # Grund (Kritiker v1): Die größten Verluste kamen in Aktien mit starkem Abwärtstrend (FMC, WULF, IT, NKE, MAN);
        # das Gitter kaufte dort Los um Los nach, bis alle am Stop-Loss endeten. Grid ist laut Idee für Seitwärtsmärkte.
        if self.adx[-1] < ADX_TREND:
            for i in range(LINIEN):
                linie = unten + i * abstand
                if vorher > linie >= kurs and len(self.trades) < LINIEN:
                    # AUSSTIEG: das Los wird an der nächsten Linie darüber verkauft (Kursziel), Stop aus risiko.py.
                    self.buy(size=LOS, sl=risiko.stop_preis(kurs), tp=linie + abstand)
