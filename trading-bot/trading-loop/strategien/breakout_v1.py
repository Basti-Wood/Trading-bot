"""breakout v1: Kaufen, wenn der Kurs mit hohem Volumen über das Hoch der letzten 20 Tage ausbricht;
Trailing-Ausstieg, wenn der Kurs unter das Tief der letzten 10 Tage fällt (Turtle-Regeln)."""
import pandas as pd
from backtesting import Strategy
import risiko

SYMBOL = "LISTE"   # Aktienliste der Bots plus eigene Aktien

EINSTIEG_TAGE = 20   # Donchian-Hoch der vorherigen 20 Tage
AUSSTIEG_TAGE = 10   # Donchian-Tief der vorherigen 10 Tage (zieht mit, wirkt als Trailing-Stop)
VOLUMEN_TAGE = 20    # "hohes Volumen" = über dem Durchschnitt der vorherigen 20 Tage


def hoch(werte, tage):
    return pd.Series(werte).rolling(tage).max().shift(1).values


def tief(werte, tage):
    return pd.Series(werte).rolling(tage).min().shift(1).values


def volumen_schnitt(werte, tage):
    return pd.Series(werte).rolling(tage).mean().shift(1).values


class Strategie(Strategy):
    def init(self):
        self.hoch = self.I(hoch, self.data.High, EINSTIEG_TAGE, name=f"Hoch {EINSTIEG_TAGE} Tage")
        self.tief = self.I(tief, self.data.Low, AUSSTIEG_TAGE, name=f"Tief {AUSSTIEG_TAGE} Tage")
        self.volumen = self.I(volumen_schnitt, self.data.Volume, VOLUMEN_TAGE, name="Volumen-Schnitt", overlay=False)

    def next(self):
        if len(self.data) < EINSTIEG_TAGE + 2:
            return
        kurs = self.data.Close[-1]
        # EINSTIEG: Schlusskurs über dem Hoch der letzten 20 Tage, Volumen über seinem 20-Tage-Durchschnitt.
        if not self.position and kurs > self.hoch[-1] and self.data.Volume[-1] > self.volumen[-1]:
            self.buy(size=0.95, sl=risiko.stop_preis(kurs))
        # AUSSTIEG: Schlusskurs unter dem Tief der letzten 10 Tage (Ausbruch gescheitert oder Trend vorbei).
        elif self.position and kurs < self.tief[-1]:
            self.position.close()
