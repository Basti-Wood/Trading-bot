"""Feste Risikoregeln für den Trading-Loop.

Diese Datei änderst nur du. Claude Code und die Agenten dürfen sie nicht bearbeiten
(Sperre in .claude/settings.json). Jeder Backtest und jede Order nutzt diese Werte.

Trag zwei Zahlen in Prozent ein, ohne %-Zeichen, mit Punkt statt Komma (7.5 heißt 7,5 %).
Welche Werte zu dir passen, entscheidest du. Keine Anlageberatung.
"""

# Verlust pro Trade, bei dem automatisch verkauft wird.
STOP_LOSS_PROZENT = 5

# Verlust vom Höchststand des Kontos, ab dem keine neuen Trades mehr starten.
MAX_DRAWDOWN_PROZENT = 10


# Ab hier nichts ändern. ------------------------------------------------------

def pruefe():
    """Bricht ab, wenn ein Wert fehlt oder unsinnig ist."""
    for name, wert in (("STOP_LOSS_PROZENT", STOP_LOSS_PROZENT),
                       ("MAX_DRAWDOWN_PROZENT", MAX_DRAWDOWN_PROZENT)):
        if wert is None:
            raise SystemExit(f"{name} fehlt. Trag den Wert in risiko.py ein und speichere.")
        if isinstance(wert, bool) or not isinstance(wert, (int, float)) or not 0 < wert < 100:
            raise SystemExit(f"{name} muss eine Zahl größer 0 und kleiner 100 sein (jetzt: {wert!r}).")


def stop_preis(einstieg, short=False):
    """Stop-Loss-Preis zu einem Einstiegspreis (long: darunter, short: darüber)."""
    pruefe()
    faktor = STOP_LOSS_PROZENT / 100
    preis = einstieg * (1 + faktor) if short else einstieg * (1 - faktor)
    return round(preis, 2) if preis >= 1 else round(preis, 4)


def drawdown_prozent(hoechststand, aktuell):
    """Wie viel Prozent das Konto unter seinem Höchststand liegt."""
    if hoechststand <= 0:
        return 0.0
    return max(0.0, (hoechststand - aktuell) / hoechststand * 100)


def drawdown_ok(hoechststand, aktuell):
    """True, solange der maximale Drawdown nicht erreicht ist."""
    pruefe()
    return drawdown_prozent(hoechststand, aktuell) < MAX_DRAWDOWN_PROZENT


if __name__ == "__main__":
    pruefe()
    print(f"Risikoregeln ok: Stop-Loss {STOP_LOSS_PROZENT} % pro Trade, "
          f"maximaler Drawdown {MAX_DRAWDOWN_PROZENT} %.")
