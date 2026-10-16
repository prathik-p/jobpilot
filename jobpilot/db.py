"""SQLite tracking. One row per unique job; status moves through the pipeline.

Statuses:
  new        -> collected, not yet filtered
  filtered   -> rejected by hard filters (reason in notes)
  scored     -> LLM scored, below queue threshold
  queued     -> good match, waiting for you to review / apply
  auto       -> eligible for auto-apply (score high + supported site)
  applied    -> application submitted
  skipped    -> you chose to skip it
  failed     -> apply attempt failed (reason in notes)
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT,
    site TEXT,
    title TEXT,
    company TEXT,
    location TEXT,
    is_remote INTEGER,
    url TEXT,
    apply_url TEXT,
    description TEXT,
    date_posted TEXT,
    min_salary REAL,
    max_salary REAL,
    status TEXT DEFAULT 'new',
    score INTEGER,
    score_json TEXT,
    cover_letter TEXT,
    notes TEXT,
    collected_at TEXT,
    updated_at TEXT,
    applied_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, started_at TEXT, finished_at TEXT, summary TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_CITY_ALIASES = {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai", "cochin": "kochi"}
_REGION_WORDS = {"india", "remote", "hybrid", "karnataka", "telangana", "kerala", "maharashtra", "tamil",
                 "andhra", "gujarat", "haryana", "west", "uttar", "madhya", "rajasthan", "punjab", "odisha",
                 "ka", "ts", "kl", "mh", "tn"}


def _city_key(location: str) -> str:
    """'Bangalore Rural, Bengaluru, India' and 'Bengaluru, Karnataka' both -> 'bengaluru'."""
    words = [_norm(part).split(" ")[0] for part in (location or "").split(",")]
    words = [_CITY_ALIASES.get(w, w) for w in words if w]
    cities = [w for w in words if w not in _REGION_WORDS]
    return min(cities) if cities else (words[0] if words else "")


def job_id(title: str, company: str, location: str = "") -> str:
    """Same title + company + city = same job, even across boards."""
    city = _city_key(location)
    key = f"{_norm(title)}|{_norm(company)}|{city}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def upsert_job(self, job: dict) -> bool:
        """Insert if new. Returns True when the job was new."""
        jid = job.get("id") or job_id(job["title"], job["company"], job.get("location", ""))
        if self.conn.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
            return False
        cols = ["id", "source", "site", "title", "company", "location", "is_remote", "url",
                "apply_url", "description", "date_posted", "min_salary", "max_salary"]
        vals = [jid] + [job.get(c) for c in cols[1:]]
        with self.tx() as c:
            c.execute(
                f"INSERT INTO jobs ({','.join(cols)}, status, collected_at, updated_at) "
                f"VALUES ({','.join('?' * len(cols))}, 'new', ?, ?)",
                vals + [now(), now()],
            )
        return True

    def set(self, jid: str, **fields):
        if not fields:
            return
        if "score_json" in fields and not isinstance(fields["score_json"], str):
            fields["score_json"] = json.dumps(fields["score_json"])
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id=?", list(fields.values()) + [jid])

    def by_status(self, *statuses: str, order: str = "score DESC") -> list[sqlite3.Row]:
        q = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE status IN ({q}) ORDER BY {order}", statuses
        ).fetchall()

    def get(self, jid: str):
        return self.conn.execute("SELECT * FROM jobs WHERE id=? OR id LIKE ?",
                                 (jid, jid + "%")).fetchone()

    def applied_today(self, site: str | None = None) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        q = "SELECT COUNT(*) FROM jobs WHERE status='applied' AND applied_at LIKE ?"
        args = [today + "%"]
        if site:
            q += " AND site=?"
            args.append(site)
        return self.conn.execute(q, args).fetchone()[0]

    def counts(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def since(self, iso: str, field: str = "updated_at") -> list[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE {field} >= ? ORDER BY score DESC", (iso,)
        ).fetchall()

    def log_run(self, kind: str, started: str, summary: dict):
        with self.tx() as c:
            c.execute("INSERT INTO runs (kind, started_at, finished_at, summary) VALUES (?,?,?,?)",
                      (kind, started, now(), json.dumps(summary)))
