"""SQLite telemetry store shared by the agents, traffic client and monitor.

WAL mode with a busy timeout so several containers can write to the same file
on one volume. Each thread gets its own connection.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from sdwan.probe import Sample

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  site TEXT NOT NULL,
  path TEXT NOT NULL,
  rtt_ms REAL,
  jitter_ms REAL,
  loss_pct REAL NOT NULL,
  throughput_mbps REAL
);
CREATE INDEX IF NOT EXISTS samples_site_path_ts ON samples(site, path, ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  site TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS traffic (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  site TEXT NOT NULL,
  ok INTEGER NOT NULL,
  failed INTEGER NOT NULL,
  avg_latency_ms REAL
);

CREATE TABLE IF NOT EXISTS path_state (
  site TEXT NOT NULL,
  path TEXT NOT NULL,
  status TEXT NOT NULL,
  active INTEGER NOT NULL,
  reason TEXT,
  updated_ts REAL NOT NULL,
  PRIMARY KEY (site, path)
);
"""

EVENT_KINDS = (
    "STARTUP",
    "DEGRADED",
    "RECOVERED",
    "FAILOVER",
    "FAILBACK",
    "ALL_DEGRADED",
    "CHAOS",
    "ERROR",
)


class Store:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        self._conn().executescript(SCHEMA)

    # -- connection handling -------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(
                self.path, timeout=5.0, isolation_level=None, check_same_thread=False
            )
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA synchronous=NORMAL")
            self._local.con = con
            with self._all_lock:
                self._all.append(con)
        return con

    def close(self) -> None:
        with self._all_lock:
            for con in self._all:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
        self._local = threading.local()

    # -- writers ------------------------------------------------------------

    def write_sample(self, site: str, sample: Sample) -> None:
        self._conn().execute(
            "INSERT INTO samples (ts, site, path, rtt_ms, jitter_ms, loss_pct, throughput_mbps)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                sample.ts,
                site,
                sample.path,
                sample.rtt_ms,
                sample.jitter_ms,
                sample.loss_pct,
                sample.throughput_mbps,
            ),
        )

    def write_event(
        self, ts: float, site: str, kind: str, path: str | None = None, detail: str | None = None
    ) -> None:
        self._conn().execute(
            "INSERT INTO events (ts, site, kind, path, detail) VALUES (?, ?, ?, ?, ?)",
            (ts, site, kind, path, detail),
        )

    def write_traffic(
        self, ts: float, site: str, ok: int, failed: int, avg_latency_ms: float | None
    ) -> None:
        self._conn().execute(
            "INSERT INTO traffic (ts, site, ok, failed, avg_latency_ms) VALUES (?, ?, ?, ?, ?)",
            (ts, site, ok, failed, avg_latency_ms),
        )

    def upsert_path_state(
        self, site: str, path: str, status: str, active: bool, reason: str | None, ts: float
    ) -> None:
        self._conn().execute(
            "INSERT INTO path_state (site, path, status, active, reason, updated_ts)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(site, path) DO UPDATE SET status=excluded.status,"
            " active=excluded.active, reason=excluded.reason, updated_ts=excluded.updated_ts",
            (site, path, status, int(active), reason, ts),
        )

    # -- readers ------------------------------------------------------------

    def latest_samples(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT s.* FROM samples s"
            " JOIN (SELECT MAX(id) AS id FROM samples GROUP BY site, path) m ON s.id = m.id"
            " ORDER BY s.site, s.path"
        ).fetchall()
        return [dict(r) for r in rows]

    def last_throughput(self) -> dict[tuple[str, str], float]:
        rows = self._conn().execute(
            "SELECT site, path, throughput_mbps FROM samples WHERE id IN"
            " (SELECT MAX(id) FROM samples WHERE throughput_mbps IS NOT NULL GROUP BY site, path)"
        ).fetchall()
        return {(r["site"], r["path"]): r["throughput_mbps"] for r in rows}

    def path_states(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM path_state ORDER BY site, path"
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, n: int = 10) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    def traffic_summary(self, seconds: float = 10, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        row = self._conn().execute(
            "SELECT COALESCE(SUM(ok), 0) AS ok, COALESCE(SUM(failed), 0) AS failed,"
            " AVG(avg_latency_ms) AS avg_latency_ms FROM traffic WHERE ts > ?",
            (now - seconds,),
        ).fetchone()
        ok, failed = int(row["ok"]), int(row["failed"])
        total = ok + failed
        return {
            "ok": ok,
            "failed": failed,
            "success_pct": (ok / total * 100.0) if total else None,
            "avg_latency_ms": row["avg_latency_ms"],
        }

    def sample_count(self) -> int:
        return int(self._conn().execute("SELECT COUNT(*) FROM samples").fetchone()[0])
