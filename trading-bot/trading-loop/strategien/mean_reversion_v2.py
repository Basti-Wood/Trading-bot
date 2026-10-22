"""mean_reversion v2: Nach einem Ausbruch unter das untere Bollinger-Band oder einem RSI unter 30 erst kaufen, wenn der
Kurs wieder steigt; verkaufen, wenn der Kurs die Mittellinie des Bollinger-Bands wieder erreicht."""
import pandas as pd
from backtesting import Strategy
import risiko

SYMBOL = "LISTE"   # Aktienliste der Bots plus eigene Aktien

BB_TAGE = 20
BB_BREITE = 2      # Standardabweichungen
RSI_TAGE = 14
RSI_GRENZE = 30
# Selbst-Anpassung: der Stresstest wählt pro Aktie Werte aus diesen Listen (Walk-forward, siehe stresstest.py).
PARAMETER = {"BB_TAGE": [10, 15, 20, 30], "BB_BREITE": [1.5, 2, 2.5], "RSI_GRENZE": [25, 30, 35]}


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
        if len(self.data) < BB_TAGE + 2:
            return
        # EINSTIEG: Gestern unter dem unteren Band ODER RSI unter 30, und heute schließt der Kurs höher als gestern.
        # Grund (Kritiker v1): 73 % der Verluste endeten am Stop-Loss nach Ø 7 Tagen = Kauf mitten in den Fall hinein.
        # Erst die bestätigte Gegenbewegung kaufen statt den fallenden Kurs.
        signal_gestern = self.data.Close[-2] < self.unten[-2] or self.rsi[-2] < RSI_GRENZE
        if not self.position and signal_gestern and kurs > self.data.Close[-2]:
            self.buy(size=0.95, sl=risiko.stop_preis(kurs))
        # AUSSTIEG: Kurs hat die Mittellinie wieder erreicht.
        elif self.position and kurs >= self.mitte[-1]:
            self.position.close()
