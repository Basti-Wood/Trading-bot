# Trading-Loop

Ein Agenten-Workflow für Claude Code. Der Nutzer beschreibt eine Trading-Idee. Der Agent `strategie` schreibt sie als Code,
der Agent `stresstest` testet sie über 5 Jahre Marktdaten, und ist der Test rot, macht der Agent `kritiker` die Autopsie,
schreibt Einstieg und Ausstieg neu und testet direkt nochmal. Jeder Fehler landet in der lokalen Datenbank `fehler.db`.
Broker ist Alpaca im Paper-Modus (Spielgeld). Die Risikoregeln stehen fest in `risiko.py`.

Antworte auf Deutsch, kurz und ohne Fachchinesisch. Der Nutzer kann nicht programmieren: Führ alle Befehle selbst aus.

## Feste Regeln (für dich und alle Agenten)
1. `risiko.py` gehört dem Nutzer. Du änderst sie nie, auch nicht über Umwege wie ein Skript. Stop-Loss und maximaler Drawdown
   aus `risiko.py` gelten für jeden Backtest und jede Order. Will der Nutzer andere Werte, öffnet er die Datei selbst
   (Mac: `open -e risiko.py`, Windows: `notepad risiko.py`).
2. Nur Paper-Modus: `TradingClient(..., paper=True)`. Echtes Geld nur, wenn der Nutzer es ausdrücklich verlangt, mit eigenen
   Live-Schlüsseln, und erst nach einer Rückfrage: „Echtes Geld, auf dein eigenes Risiko. Sicher?“
3. `.env` liest du nie und gibst Schlüssel nie aus. Code lädt sie mit python-dotenv (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`).
4. Vor jedem neuen Versuch: `python fehler.py lesen`. Was dort steht, wird nicht wiederholt.
5. Keine Anlageberatung: Du empfiehlst keine Strategien, Werte oder Zahlen für die Risikoregeln und versprichst keine Rendite.
6. Python immer aus `.venv` (Mac: `.venv/bin/python`, Windows: `.venv\Scripts\python.exe`). Wo hier `python` steht, ist das gemeint.

## Einrichtung („Richte das Projekt ein“)
1. Python 3.10 oder neuer prüfen. Fehlt es oder ist es älter: Sag dem Nutzer, er soll Python von https://www.python.org/downloads/
   installieren und dann „weiter“ schreiben.
2. `.venv` anlegen und darin installieren: `alpaca-py`, `backtesting`, `python-dotenv`, `pandas`.
3. `python fehler.py anlegen`.
4. `.env` für den Nutzer öffnen (Mac: `open -e .env`, Windows: `notepad .env`). Er trägt seinen API Key und Secret Key aus dem
   Alpaca-Paper-Konto ein und speichert. Klappt das Öffnen nicht, soll er `! open -e .env` (Windows `! notepad .env`) tippen.
   Warte, bis er „fertig“ schreibt.
5. Genauso `risiko.py` öffnen: Er trägt `STOP_LOSS_PROZENT` und `MAX_DRAWDOWN_PROZENT` ein und speichert. Dann `python risiko.py`.
   Meldet das einen Fehler, erklär ihm in einem Satz, was er korrigieren muss.
6. Verbindung testen mit einem kleinen Skript `verbindung.py`: Paper-Konto abrufen (Status, Kontowert) und die letzten
   5 Tageskerzen von SPY über `StockHistoricalDataClient` mit `feed=DataFeed.IEX` laden. Keine Schlüssel ausgeben.
7. Melde in drei Zeilen: installiert, Paper-Konto verbunden (Kontowert), Risikoregeln. Dann: „Schick mir deine Trading-Idee.“

## Der Loop (der Nutzer schickt eine Trading-Idee)
1. Fehlt etwas Wichtiges (was gehandelt wird, wann Einstieg, wann Ausstieg), frag einmal kurz nach. Sonst nichts erfinden.
2. `python fehler.py lesen`.
3. Agent `strategie`: schreibt `strategien/<name>_v1.py`.
4. Agent `stresstest`: Backtest über die letzten 5 Jahre, Ergebnis grün oder rot.
5. Rot: Agent `kritiker`. Er macht die Autopsie, schreibt `<name>_v2.py`, testet direkt nochmal und speichert den Fehler.
   Wiederholen, bis eine Version grün ist, höchstens 5 Versionen pro Idee. Danach hörst du auf, statt die Vergangenheit
   schönzurechnen, und sagst ehrlich, dass die Idee im Test nicht trägt.
6. Bericht an den Nutzer: eine Zeile pro Version (Rendite, Kaufen-und-Halten im selben Zeitraum, max. Drawdown, Trades,
   grün/rot, was geändert wurde) und wo die Charts liegen (`berichte/`). Nichts live schalten.

## Grün oder rot
Rot, sobald eins zutrifft: Gesamtrendite 0 % oder weniger · maximaler Drawdown über `MAX_DRAWDOWN_PROZENT` · weniger als
10 Trades (zu wenig, um etwas zu sagen). Sonst grün. So steht es in `urteil()` in `stresstest.py`.

Bei `SYMBOL = "LISTE"` (Wunsch des Nutzers: Aktienliste der Bots plus eigene Aktien, `python liste.py`) gilt `urteil()` für jede
Aktie einzeln. Es gibt keine Mindestanzahl grüner Aktien und kein Listenurteil als Kaufhürde. Jede Aktie bekommt ihre
Kennzahlen und ihr eigenes Grün/Rot im JSON-Bericht. Die letzte Zeile zählt nur die grünen Einzeltests. Der Bot vergleicht pro
Aktie alle aktiven Versionen: höchste Rendite, dann kleinster absoluter Drawdown. Nur ein BUY dieser besten grünen Version kann
kaufen. Ein niedriger bewertetes BUY überstimmt sie nicht. Eigene externe Aktien bekommen nur Signale. Der Bot wiederholt den
Stresstest täglich ab 8:00 New Yorker Zeit und auf Wunsch im Dashboard, ohne Strategie-Dateien zu ändern.

## Selbst-Anpassung (Wunsch des Nutzers)
Jede Strategie bekommt `PARAMETER = {"NAME": [Werte, ...]}` für ihre eigenen Konstanten (höchstens 10 Werte pro Name, nur
Konstanten, aus denen keine andere Konstante berechnet wird; der Standardwert gehört in die Liste). Der Stresstest wählt dann pro
Aktie selbst: Für jedes der letzten 2 Jahre sucht er die besten Werte nur in den Jahren davor und handelt damit das unbekannte Jahr.
Grün oder rot entscheidet nur dieses Ergebnis der letzten 2 Jahre (gleiche Regeln, mindestens 4 Trades statt 10). Die Werte für den
Live-Handel wählt er über alle 5 Jahre. Der Bot passt jeden Morgen neu an. Die Datei selbst ändert sich nie; die gewählten Werte
stehen im Bericht (`parameter`, `standard`, `test.jahre`). Ohne `PARAMETER` gilt wie bisher der 5-Jahres-Test. Der Kritiker
darf keine Listen wählen, die nur die Vergangenheit schönrechnen: wenige, übliche Werte rund um die Idee des Nutzers.

## Im Trading-Bot (Standardweg für Live im Paper-Modus)
Dieser Ordner liegt im Trading-Bot (`trading-bot/trading-loop`). Der Bot handelt Versionen aus diesem Ordner als dritten Bot
„Trading-Loop“. Bot-Code (`../app`, `../dashboard`, `../tests`) änderst du nur, wenn der Nutzer es ausdrücklich verlangt.
- `stresstest.py` schreibt dafür zusätzlich `berichte/<name>_vN.json` (Kennzahlen, Ergebnis, Prüfsumme der Datei, Werte aus
  `risiko.py`). Einschalten lässt sich im Dashboard eine getestete Version, die seit dem Test unverändert ist und mit den
  aktuellen Werten aus `risiko.py` getestet wurde. Das gilt auch für bisher rote Listenberichte. Nach Dateiänderungen neu testen.
  Mehrere Versionen derselben Idee dürfen gleichzeitig aktiv sein; jede Aktie wählt ihre beste grüne Version separat.
- Der Nutzer schaltet Versionen im Dashboard selbst ein und gibt dem Trading-Loop dort eine Gewichtung. Du schaltest nichts ein.
- Sagt der Nutzer „Bring … live“, verweis ihn auf das Dashboard (Bereich Trading-Loop). `live.py` nur, wenn er ausdrücklich ein
  eigenes Skript will, und dann nie gleichzeitig mit dem Bot für dasselbe Alpaca-Konto.

## Live als eigenes Skript (nur wenn der Nutzer es ausdrücklich so will)
Schreib `live.py` für die gewählte Version:
- Läuft im Terminal, rechnet im Takt der Strategie (bei Tageskerzen einmal pro Handelstag) dieselben Signale wie im Backtest.
- Schickt Orders an das Paper-Konto, jede Kauforder mit Stop-Loss beim Broker: `MarketOrderRequest` mit
  `order_class=OrderClass.OTO` und `stop_loss=StopLossRequest(stop_price=risiko.stop_preis(einstieg))`.
- Merkt sich den Höchststand des Kontowerts in `zustand.json`. Vor jeder Order `risiko.drawdown_ok(hoechststand, kontowert)`.
  Ist der maximale Drawdown erreicht: keine neuen Orders, offene Positionen schließen, Meldung ausgeben, beenden.
- Schreibt jede Aktion mit Uhrzeit in `berichte/live.log`.
Starte `live.py` nicht selbst. Sag dem Nutzer, wie er es in einem eigenen Terminal-Fenster startet und stoppt (ctrl + C).

## Ordner
- `strategien/` eine Datei pro Version · `berichte/` Ergebnisse (.txt, .json), Trades und Charts · `daten/` zwischengespeicherte Kurse
- `stresstest.py` Backtest über 5 Jahre · `fehler.db` Fehler-Datenbank (über `fehler.py`)
- `risiko.py` Risikoregeln (nur der Nutzer) · `.env` Schlüssel (nur der Nutzer)
