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
    fallback_from TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_day ON calls(day);
"""


def utc_day(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path=":memory:", clock=time.time):
        self.path = path
        self.clock = clock
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def record(self, provider, ok, error=None, tokens_in=0, tokens_out=0,
               cost=0.0, latency_ms=None, feature="default", fallback_from=None):
        ts = self.clock()
        cur = self.conn.execute(
            "INSERT INTO calls (ts, day, provider, ok, error, tokens_in, tokens_out,"
            " cost, latency_ms, feature, fallback_from)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ts, utc_day(ts), provider, 1 if ok else 0, error, tokens_in,
             tokens_out, cost, latency_ms, feature, fallback_from))
        self.conn.commit()
        return cur.lastrowid

    def spend_today(self):
        """Total cost logged for the current UTC day."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS total FROM calls WHERE day = ?",
            (utc_day(self.clock()),)).fetchone()
        return float(row["total"])

    def all_calls(self):
        rows = self.conn.execute("SELECT * FROM calls ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
