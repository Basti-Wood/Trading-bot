"""Fehler-Datenbank des Trading-Loops (lokal, SQLite-Datei fehler.db in diesem Ordner).

Jede gescheiterte Strategie-Version landet hier. Die Agenten lesen die Einträge vor jedem
neuen Versuch, damit sie keinen Fehler zweimal machen.

  python fehler.py anlegen
  python fehler.py lesen [--strategie NAME]
  python fehler.py speichern --strategie NAME --version 2 --problem "…" --aenderung "…"
                             [--verlust-trades 14] [--ergebnis rot|gruen]
"""
import argparse
import datetime
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent / "fehler.db"

SCHEMA = """CREATE TABLE IF NOT EXISTS fehler (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    datum TEXT NOT NULL,
    strategie TEXT NOT NULL,
    version INTEGER NOT NULL,
    problem TEXT NOT NULL,
    verlust_trades INTEGER,
    aenderung TEXT NOT NULL,
    ergebnis TEXT
)"""


def verbinden():
    con = sqlite3.connect(DB)
    con.execute(SCHEMA)
    return con


def anlegen(_):
    verbinden().close()
    print(f"Fehler-Datenbank bereit: {DB.name}")


def lesen(a):
    con = verbinden()
    sql = "SELECT datum, strategie, version, problem, verlust_trades, aenderung, ergebnis FROM fehler"
    werte = ()
    if a.strategie:
        sql += " WHERE strategie = ?"
        werte = (a.strategie,)
    zeilen = con.execute(sql + " ORDER BY id DESC", werte).fetchall()
    con.close()
    if not zeilen:
        print("Noch keine Fehler gespeichert.")
        return
    print(f"{len(zeilen)} Fehler gespeichert (neueste zuerst):")
    for datum, strategie, version, problem, verlust, aenderung, ergebnis in zeilen:
        trades = f", {verlust} Verlust-Trades" if verlust is not None else ""
        print(f"- {datum} · {strategie} v{version}{trades}")
        print(f"  Problem: {problem}")
        print(f"  Geändert: {aenderung}" + (f" → danach {ergebnis}" if ergebnis else ""))


def speichern(a):
    con = verbinden()
    with con:
        cur = con.execute(
            "INSERT INTO fehler (datum, strategie, version, problem, verlust_trades, aenderung, ergebnis)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.datetime.now().isoformat(timespec="minutes"), a.strategie, a.version,
             a.problem, a.verlust_trades, a.aenderung, a.ergebnis))
    con.close()
    print(f"Gespeichert als Fehler #{cur.lastrowid}.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fehler-Datenbank des Trading-Loops")
    sub = ap.add_subparsers(dest="befehl", required=True)
    sub.add_parser("anlegen").set_defaults(f=anlegen)
    p = sub.add_parser("lesen")
    p.add_argument("--strategie")
    p.set_defaults(f=lesen)
    p = sub.add_parser("speichern")
    p.add_argument("--strategie", required=True)
    p.add_argument("--version", type=int, required=True)
    p.add_argument("--problem", required=True)
    p.add_argument("--aenderung", required=True)
    p.add_argument("--verlust-trades", dest="verlust_trades", type=int)
    p.add_argument("--ergebnis", choices=["rot", "gruen"])
    p.set_defaults(f=speichern)
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    sys.exit(main())
