"""grid v1: Gitter. Die Spanne zwischen Hoch und Tief der letzten 20 Tage wird in 10 Linien geteilt.
Fällt der Kurs auf eine Linie, kauft die Strategie ein Los; steigt er bis zur nächsten Linie darüber, verkauft sie es wieder."""
import pandas as pd
from backtesting import Strategy
import risiko

SYMBOL = "LISTE"   # Aktienliste der Bots plus eigene Aktien

SPANNE_TAGE = 20   # Spanne wandert mit: Hoch und Tief der vorherigen 20 Tage
LINIEN = 10
LOS = 0.95 / LINIEN   # Anteil des Kapitals pro Linie, alle Linien zusammen höchstens 95 %


def hoch(werte, tage):
    return pd.Series(werte).rolling(tage).max().shift(1).values


def tief(werte, tage):
    return pd.Series(werte).rolling(tage).min().shift(1).values


class Strategie(Strategy):
    def init(self):
        self.oben = self.I(hoch, self.data.High, SPANNE_TAGE, name="Gitter oben")
        self.unten = self.I(tief, self.data.Low, SPANNE_TAGE, name="Gitter unten")

    def next(self):
        if len(self.data) < SPANNE_TAGE + 2:
            return
        oben, unten = self.oben[-1], self.unten[-1]
        abstand = (oben - unten) / LINIEN
        if not abstand > 0:
            return
        vorher, kurs = self.data.Close[-2], self.data.Close[-1]
        # EINSTIEG: für jede Gitterlinie, die der Kurs heute von oben nach unten erreicht hat, ein Los kaufen.
        for i in range(LINIEN):
            linie = unten + i * abstand
            if vorher > linie >= kurs and len(self.trades) < LINIEN:
                # AUSSTIEG: das Los wird an der nächsten Linie darüber verkauft (Kursziel), Stop aus risiko.py.
                self.buy(size=LOS, sl=risiko.stop_preis(kurs), tp=linie + abstand)
