"""intraday_trend v1: Trend auf 5-Minuten-Kerzen. Kaufen, wenn der EMA 9 den EMA 21 von unten kreuzt und der Kurs über
dem VWAP des Tages liegt; verkaufen, wenn der EMA 9 den EMA 21 von oben kreuzt oder der Kurs unter den VWAP fällt.
Vor jedem Börsenschluss ist die Position zu (Stresstest und Intraday-Loop), nie über Nacht."""
import numpy as np
import pandas as pd
from backtesting import Strategy
from backtesting.lib import crossover
import risiko

SYMBOL = "LISTE"     # Aktienliste der Bots plus eigene Aktien
ZEITRAHMEN = "5Min"  # Intraday: handelt der Intraday-Loop, Signal nach jeder abgeschlossenen Kerze

SCHNELL = 9
LANGSAM = 21
START_KERZEN = 3     # die ersten 15 Minuten nach der Eröffnung abwarten (unruhige Eröffnung)
# Selbst-Anpassung: der Stresstest wählt pro Aktie Werte aus diesen Listen (Walk-forward, siehe stresstest.py).
PARAMETER = {"SCHNELL": [5, 9, 13], "LANGSAM": [21, 34, 55], "START_KERZEN": [1, 3, 6]}


def ema(werte, kerzen):
    return pd.Series(werte).ewm(span=kerzen, adjust=False).mean().values


def vwap(hoch, tief, schluss, volumen, tage):
    """Volumengewichteter Durchschnittspreis, jeden Handelstag neu ab der Eröffnung."""
    typisch = pd.Series((np.asarray(hoch) + np.asarray(tief) + np.asarray(schluss)) / 3)
    volumen = pd.Series(np.asarray(volumen, dtype=float))
    gruppen = pd.Series(np.asarray(tage))
    summe = (typisch * volumen).groupby(gruppen).cumsum()
    menge = volumen.groupby(gruppen).cumsum().replace(0, np.nan)
    return (summe / menge).fillna(typisch).values


def kerze_im_tag(tage):
    return pd.Series(np.asarray(tage)).groupby(np.asarray(tage)).cumcount().values


class Strategie(Strategy):
    def init(self):
        tage = self.data.index.normalize()
        self.schnell = self.I(ema, self.data.Close, SCHNELL, name=f"EMA {SCHNELL}")
        self.langsam = self.I(ema, self.data.Close, LANGSAM, name=f"EMA {LANGSAM}")
        self.vwap = self.I(vwap, self.data.High, self.data.Low, self.data.Close, self.data.Volume, tage, name="VWAP")
        self.nummer = self.I(kerze_im_tag, tage, name="Kerze im Tag", plot=False)

    def next(self):
        if self.nummer[-1] < START_KERZEN:
            return
        kurs = self.data.Close[-1]
        # EINSTIEG: EMA schnell kreuzt EMA langsam nach oben UND der Kurs liegt über dem VWAP des Tages.
        if not self.position and crossover(self.schnell, self.langsam) and kurs > self.vwap[-1]:
            self.buy(size=0.95, sl=risiko.stop_preis(kurs))
        # AUSSTIEG: EMA schnell kreuzt nach unten ODER der Kurs fällt unter den VWAP. Nur long: schließen statt short.
        elif self.position and (crossover(self.langsam, self.schnell) or kurs < self.vwap[-1]):
            self.position.close()
