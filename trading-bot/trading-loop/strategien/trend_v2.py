"""trend v2: Trendfolge. Kaufen, wenn der EMA 20 den EMA 50 von unten kreuzt und der ADX einen Trend bestätigt;
verkaufen, wenn der EMA 20 den EMA 50 von oben kreuzt."""
import pandas as pd
from backtesting import Strategy
from backtesting.lib import crossover
import risiko

SYMBOL = "LISTE"   # Aktienliste der Bots plus eigene Aktien

SCHNELL = 20
LANGSAM = 50
ADX_TAGE = 14
ADX_TREND = 20     # üblicher Schwellenwert laut Quellen: über 20 gilt als Trend
# Selbst-Anpassung: der Stresstest wählt pro Aktie Werte aus diesen Listen (Walk-forward, siehe stresstest.py).
PARAMETER = {"SCHNELL": [10, 20, 30], "LANGSAM": [50, 80, 100, 150], "ADX_TREND": [15, 20, 25]}


def ema(werte, tage):
    return pd.Series(werte).ewm(span=tage, adjust=False).mean().values


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
        self.schnell = self.I(ema, self.data.Close, SCHNELL, name=f"EMA {SCHNELL}")
        self.langsam = self.I(ema, self.data.Close, LANGSAM, name=f"EMA {LANGSAM}")
        self.adx = self.I(adx, self.data.High, self.data.Low, self.data.Close, ADX_TAGE, name=f"ADX {ADX_TAGE}", overlay=False)

    def next(self):
        if len(self.data) < LANGSAM:
            return
        # EINSTIEG: EMA 20 kreuzt EMA 50 nach oben UND ADX über 20.
        # Grund (Kritiker v1): 75 % der Trades verloren, meist schnell am Stop-Loss (Ø 28 Tage) = Fehlsignale ohne echten
        # Trend. Der ADX-Filter aus der Idee des Nutzers lässt Kreuzungen in trendlosen Phasen aus.
        if not self.position and crossover(self.schnell, self.langsam) and self.adx[-1] > ADX_TREND:
            self.buy(size=0.95, sl=risiko.stop_preis(self.data.Close[-1]))
        # AUSSTIEG: schneller Durchschnitt kreuzt den langsamen von oben nach unten. Nur long: schließen statt short.
        elif self.position and crossover(self.langsam, self.schnell):
            self.position.close()
