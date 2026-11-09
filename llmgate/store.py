"""SQLite call log. One row per provider attempt (including failed ones)."""

import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    day           TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    ok            INTEGER NOT NULL,
    error         TEXT,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    cost          REAL    NOT NULL DEFAULT 0,
    latency_ms    REAL,
    feature       TEXT    NOT NULL DEFAULT 'default',
    fallback_from TEXT,
    request_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_day ON calls(day);
"""

# Columns added after the first release. Old databases get them on open.
MIGRATIONS = {"request_id": "ALTER TABLE calls ADD COLUMN request_id TEXT"}


def utc_day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path=":memory:", clock=time.time):
        self.path = path
        self.clock = clock
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(calls)")}
        for col, ddl in MIGRATIONS.items():
            if col not in cols:
                self.conn.execute(ddl)
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calls_provider ON calls(provider, id)")
        self.conn.commit()

    def record(self, provider, ok, error=None, tokens_in=0, tokens_out=0,
               cost=0.0, latency_ms=None, feature="default", fallback_from=None,
               request_id=None):
        ts = self.clock()
        cur = self.conn.execute(
            "INSERT INTO calls (ts, day, provider, ok, error, tokens_in, tokens_out,"
            " cost, latency_ms, feature, fallback_from, request_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, utc_day(ts), provider, 1 if ok else 0, error, tokens_in,
             tokens_out, cost, latency_ms, feature, fallback_from, request_id))
        self.conn.commit()
        return cur.lastrowid

    def spend_today(self):
        """Total cost logged for the current UTC day."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS total FROM calls WHERE day = ?",
            (utc_day(self.clock()),)).fetchone()
        return float(row["total"])

    def recent_attempts(self, provider, n):
        """The provider's last n attempts, newest first, as (ok, ts) pairs."""
        rows = self.conn.execute(
            "SELECT ok, ts FROM calls WHERE provider = ? ORDER BY id DESC LIMIT ?",
            (provider, n)).fetchall()
        return [(bool(r["ok"]), r["ts"]) for r in rows]

    def all_calls(self):
        rows = self.conn.execute("SELECT * FROM calls ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
