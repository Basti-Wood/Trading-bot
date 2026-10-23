"""breakout v2: Kaufen, wenn der Kurs mit hohem Volumen über das Hoch der letzten 55 Tage ausbricht;
Trailing-Ausstieg, wenn der Kurs unter das Tief der letzten 20 Tage fällt (Turtle-System 2)."""
import pandas as pd
from backtesting import Strategy
import risiko

SYMBOL = "LISTE"   # Aktienliste der Bots plus eigene Aktien

EINSTIEG_TAGE = 55   # Turtle-System 2: Donchian-Hoch der vorherigen 55 Tage
AUSSTIEG_TAGE = 20   # Turtle-System 2: Donchian-Tief der vorherigen 20 Tage (zieht mit, wirkt als Trailing-Stop)
VOLUMEN_TAGE = 20    # "hohes Volumen" = über dem Durchschnitt der vorherigen 20 Tage
# Selbst-Anpassung: der Stresstest wählt pro Aktie Werte aus diesen Listen (Walk-forward, siehe stresstest.py).
PARAMETER = {"EINSTIEG_TAGE": [30, 40, 55, 70], "AUSSTIEG_TAGE": [10, 15, 20, 30], "VOLUMEN_TAGE": [10, 20]}


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
        # EINSTIEG: Schlusskurs über dem Hoch der letzten 55 Tage, Volumen über seinem 20-Tage-Durchschnitt.
        # Grund (Kritiker v1): 65 % der Trades verloren, Verluste nach Ø 16 Tagen = gescheiterte Ausbrüche aus kurzen
        # Spannen. Das dokumentierte Turtle-System 2 verlangt einen Ausbruch aus einer längeren Spanne.
        if not self.position and kurs > self.hoch[-1] and self.data.Volume[-1] > self.volumen[-1]:
            self.buy(size=0.95, sl=risiko.stop_preis(kurs))
        # AUSSTIEG: Schlusskurs unter dem Tief der letzten 20 Tage (passend zu System 2).
        elif self.position and kurs < self.tief[-1]:
            self.position.close()
