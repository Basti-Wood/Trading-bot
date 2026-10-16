"""Small durable journal shared by the dashboard, engine and isolated trainer."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "bot.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, kind TEXT, at TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS orders (client_id TEXT PRIMARY KEY, at TEXT, body TEXT);
                CREATE TABLE IF NOT EXISTS experience (
                    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, feed TEXT NOT NULL,
                    feature_schema INTEGER NOT NULL, bar_at TEXT NOT NULL,
                    captured_at TEXT NOT NULL, model_version TEXT NOT NULL,
                    features TEXT NOT NULL, predictions TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', outcome TEXT, resolved_at TEXT,
                    UNIQUE(symbol, feed, feature_schema, bar_at)
                );
                CREATE INDEX IF NOT EXISTS experience_status
                    ON experience(symbol, feed, feature_schema, status, bar_at);
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=20)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value, allow_nan=False)))

    def event(self, kind, body):
        with self.db() as db:
            db.execute("INSERT INTO journal(kind,at,body) VALUES (?,?,?)", (kind, utcnow(), json.dumps(body, allow_nan=False)))
            db.execute("DELETE FROM journal WHERE kind=? AND id NOT IN (SELECT id FROM journal WHERE kind=? ORDER BY id DESC LIMIT 2000)", (kind, kind))

    def history(self, kind, limit=100):
        with self.db() as db:
            rows = db.execute("SELECT at,body FROM journal WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
        return [{"at": at, **json.loads(body)} for at, body in rows]

    def reserve_order(self, client_id, body, strategy=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if strategy and body["side"] == "buy":
                owner = self._owner(db, body["symbol"])
                if owner and owner != strategy:
                    return False
            result = db.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?)", (client_id, utcnow(), json.dumps(body)))
            if result.rowcount and strategy and body["side"] == "buy":
                db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (f"position_owner:{body['symbol']}", json.dumps(strategy)))
        return result.rowcount == 1

    @staticmethod
    def _owner(db, symbol):
        row = db.execute("SELECT value FROM state WHERE key=?", (f"position_owner:{symbol}",)).fetchone()
        if row:
            return json.loads(row[0])
        # Existing daily holdings retain their owner when parallel mode is added.
        legacy = db.execute("SELECT value FROM state WHERE key=?", (f"owned:{symbol}",)).fetchone()
        return "daily" if legacy and json.loads(legacy[0]) else None

    def owner(self, symbol):
        with self.db() as db:
            return self._owner(db, symbol)

    def owned_symbols(self, strategy):
        with self.db() as db:
            rows = db.execute("SELECT key FROM state WHERE key LIKE 'position_owner:%' OR key LIKE 'owned:%'").fetchall()
            symbols = {row[0].split(":", 1)[1] for row in rows}
            return sorted(s for s in symbols if self._owner(db, s) == strategy)

    def set_owned(self, symbol, strategy, owned):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._owner(db, symbol)
            if current and current != strategy:
                if owned:
                    raise ValueError(f"Symbol belongs to the {current} strategy")
                return
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (f"position_owner:{symbol}", json.dumps(strategy if owned else None)))
            if strategy == "daily":
                db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (f"owned:{symbol}", json.dumps(owned)))

    def update_order(self, client_id, body):
        with self.db() as db:
            db.execute("UPDATE orders SET body=? WHERE client_id=?", (json.dumps(body), client_id))

    def order(self, client_id):
        with self.db() as db:
            row = db.execute("SELECT body FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def orders(self, limit=100):
        with self.db() as db:
            rows = db.execute("SELECT client_id,at,body FROM orders ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
        return [{"client_id": cid, "at": at, **json.loads(body)} for cid, at, body in rows]

    def unresolved_orders(self):
        with self.db() as db:
            rows = db.execute("SELECT client_id,at,body FROM orders WHERE json_extract(body,'$.status') NOT IN ('filled','canceled','expired','rejected','replaced','done_for_day')").fetchall()
        return [{"client_id": cid, "at": at, **json.loads(body)} for cid, at, body in rows]

    def session_entries(self, symbol, session):
        with self.db() as db:
            rows = db.execute("SELECT client_id,at,body FROM orders WHERE json_extract(body,'$.symbol')=? AND json_extract(body,'$.session')=? AND json_extract(body,'$.side')='buy' ORDER BY at", (symbol, session)).fetchall()
        return [{"client_id": cid, "at": at, **json.loads(body)} for cid, at, body in rows]

    def capture_experience(self, record):
        """Keep the first prediction per completed bar, including across restarts."""
        with self.db() as db:
            result = db.execute("""INSERT OR IGNORE INTO experience
                (symbol,feed,feature_schema,bar_at,captured_at,model_version,features,predictions)
                VALUES (?,?,?,?,?,?,?,?)""", (
                record["symbol"], record["feed"], record["feature_schema"], record["bar_at"],
                record["captured_at"], record["model_version"],
                json.dumps(record["features"], allow_nan=False), json.dumps(record["predictions"], allow_nan=False)))
            # Bounded replay data, separate from the rotating decision journal.
            db.execute("""DELETE FROM experience WHERE symbol=? AND feed=? AND feature_schema=?
                AND id NOT IN (SELECT id FROM experience WHERE symbol=? AND feed=? AND feature_schema=?
                               ORDER BY bar_at DESC LIMIT 2000)""", (record["symbol"], record["feed"], record["feature_schema"]) * 2)
        return result.rowcount == 1

    def experience(self, symbol, feed, feature_schema, status=None):
        query = "SELECT * FROM experience WHERE symbol=? AND feed=? AND feature_schema=?"
        params = [symbol, feed, feature_schema]
        if status:
            query += " AND status=?"
            params.append(status)
        with self.db() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(query + " ORDER BY bar_at DESC LIMIT 2000", params).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            for field in ("features", "predictions", "outcome"):
                item[field] = json.loads(item[field]) if item[field] else None
            results.append(item)
        return results

    def resolve_experience(self, record_id, status, outcome):
        if status not in {"resolved", "excluded"}:
            raise ValueError("Invalid experience outcome status")
        with self.db() as db:
            result = db.execute("""UPDATE experience SET status=?,outcome=?,resolved_at=?
                WHERE id=? AND status='pending'""", (status, json.dumps(outcome, allow_nan=False), utcnow(), record_id))
        return result.rowcount == 1


class StrategyStore:
    """Strategy-local state over one account journal and ownership registry."""
    SHARED = {"account", "account_scope", "loss_halt_day", "equity_sample", "universe", "scan_status", "allocation",
              "crypto_universe", "crypto_scan_status"}

    def __init__(self, root, strategy):
        self.root, self.strategy = root, strategy
        self.prefix = "" if strategy == "daily" else f"{strategy}:"

    def __getattr__(self, name):
        return getattr(self.root, name)

    def key(self, key):
        return key if key in self.SHARED else self.prefix + key

    def get(self, key, default=None):
        if key.startswith("owned:"):
            return self.root.owner(key[6:]) == self.strategy
        return self.root.get(self.key(key), default)

    def set(self, key, value):
        if key.startswith("owned:"):
            self.root.set_owned(key[6:], self.strategy, value)
        else:
            self.root.set(self.key(key), value)

    def event(self, kind, body):
        self.root.event(kind, {**body, "strategy": self.strategy})

    def history(self, kind, limit=100):
        with self.root.db() as db:
            rows = db.execute("SELECT at,body FROM journal WHERE kind=? AND COALESCE(json_extract(body,'$.strategy'),'daily')=? ORDER BY id DESC LIMIT ?", (kind, self.strategy, limit)).fetchall()
        return [{"at": at, **json.loads(body)} for at, body in rows]

    def reserve_order(self, client_id, body):
        return self.root.reserve_order(client_id, {**body, "strategy": self.strategy}, strategy=self.strategy)

    def session_entries(self, symbol, session):
        return [o for o in self.root.session_entries(symbol, session) if o.get("strategy", "daily") == self.strategy]

    def owns_order(self, order):
        record = self.root.order(order.get("client_order_id", ""))
        return bool(record and record.get("strategy", "daily") == self.strategy)

    def owned_symbols(self):
        return self.root.owned_symbols(self.strategy)


def store_for_config(config):
    root = Store(config.data_dir)
    return StrategyStore(root, config.trading_mode) if config.coordinated else root
