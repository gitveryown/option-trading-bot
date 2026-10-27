"""Single writer SQLite checkpoint and audit ledger."""
import json
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path, cfg):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT);
        CREATE TABLE IF NOT EXISTS frames (id TEXT PRIMARY KEY, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);
        """)
        config = json.loads(json.dumps(cfg.serial()))
        with self.transaction():
            row = self.db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()
            if row:
                if json.loads(row[0])["config"] != config:
                    raise ValueError("Database configuration differs; use a separate database")
            else:
                state = dict(config=config, cash=str(cfg.capital), unsettled="0.00",
                             positions={}, day=None, day_equity=str(cfg.capital),
                             peak_equity=str(cfg.capital), entries=0, loss_streak=0,
                             day_halt=None, halt=None, last_at=None)
                self.save(state)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def state(self):
        return json.loads(self.db.execute("SELECT body FROM checkpoint WHERE id=1").fetchone()[0])

    def save(self, state):
        self.db.execute("INSERT OR REPLACE INTO checkpoint VALUES(1, ?)",
                        (json.dumps(state, sort_keys=True),))

    def event(self, at, kind, **body):
        self.db.execute("INSERT INTO events(at,kind,body) VALUES(?,?,?)",
                        (at, kind, json.dumps(body, default=str, sort_keys=True)))

    def events(self):
        return [dict(id=r[0], at=r[1], kind=r[2], **json.loads(r[3]))
                for r in self.db.execute("SELECT id,at,kind,body FROM events ORDER BY id")]

    def close(self):
        self.db.close()
