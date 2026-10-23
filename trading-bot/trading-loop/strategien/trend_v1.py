"""trend v1: Trendfolge. Kaufen, wenn der EMA 20 den EMA 50 von unten kreuzt; verkaufen, wenn er ihn von oben kreuzt."""
import pandas as pd
from backtesting import Strategy
from backtesting.lib import crossover
import risiko

SYMBOL = "LISTE"   # Aktienliste der Bots plus eigene Aktien

SCHNELL = 20
LANGSAM = 50
# Selbst-Anpassung: der Stresstest wählt pro Aktie Werte aus diesen Listen (Walk-forward, siehe stresstest.py).
PARAMETER = {"SCHNELL": [10, 15, 20, 30], "LANGSAM": [40, 50, 80, 100, 150, 200]}


def ema(werte, tage):
    return pd.Series(werte).ewm(span=tage, adjust=False).mean().values


class Strategie(Strategy):
    def init(self):
        self.schnell = self.I(ema, self.data.Close, SCHNELL, name=f"EMA {SCHNELL}")
        self.langsam = self.I(ema, self.data.Close, LANGSAM, name=f"EMA {LANGSAM}")

    def next(self):
        if len(self.data) < LANGSAM:
            return
        # EINSTIEG: schneller Durchschnitt kreuzt den langsamen von unten nach oben (Bullentrend).
        if not self.position and crossover(self.schnell, self.langsam):
            self.buy(size=0.95, sl=risiko.stop_preis(self.data.Close[-1]))
        # AUSSTIEG: schneller Durchschnitt kreuzt den langsamen von oben nach unten. Nur long: schließen statt short.
        elif self.position and crossover(self.langsam, self.schnell):
            self.position.close()
