"""mean_reversion v1: Kaufen, wenn der Kurs unter das untere Bollinger-Band fällt oder der RSI unter 30 liegt;
verkaufen, wenn der Kurs die Mittellinie des Bollinger-Bands wieder erreicht."""
import pandas as pd
from backtesting import Strategy
import risiko

SYMBOL = "LISTE"   # Aktienliste der Bots plus eigene Aktien

BB_TAGE = 20
BB_BREITE = 2      # Standardabweichungen
RSI_TAGE = 14
RSI_GRENZE = 30


def mittellinie(werte, tage):
    return pd.Series(werte).rolling(tage).mean().values


def unteres_band(werte, tage, breite):
    reihe = pd.Series(werte)
    return (reihe.rolling(tage).mean() - breite * reihe.rolling(tage).std(ddof=0)).values


def rsi(werte, tage):
    """RSI nach Wilder (geglätteter Durchschnitt der Gewinne und Verluste)."""
    diff = pd.Series(werte).diff()
    gewinn = diff.clip(lower=0).ewm(alpha=1 / tage, adjust=False, min_periods=tage).mean()
    verlust = (-diff.clip(upper=0)).ewm(alpha=1 / tage, adjust=False, min_periods=tage).mean()
    return (100 - 100 / (1 + gewinn / verlust)).values


class Strategie(Strategy):
    def init(self):
        self.mitte = self.I(mittellinie, self.data.Close, BB_TAGE, name="Bollinger Mitte")
        self.unten = self.I(unteres_band, self.data.Close, BB_TAGE, BB_BREITE, name="Bollinger unten")
        self.rsi = self.I(rsi, self.data.Close, RSI_TAGE, name=f"RSI {RSI_TAGE}")

    def next(self):
        kurs = self.data.Close[-1]
        if len(self.data) < BB_TAGE + 1:
            return
        # EINSTIEG: Ausbruch unter das untere Band ODER RSI überverkauft (unter 30).
        if not self.position and (kurs < self.unten[-1] or self.rsi[-1] < RSI_GRENZE):
            self.buy(size=0.95, sl=risiko.stop_preis(kurs))
        # AUSSTIEG: Kurs hat die Mittellinie wieder erreicht.
        elif self.position and kurs >= self.mitte[-1]:
            self.position.close()
