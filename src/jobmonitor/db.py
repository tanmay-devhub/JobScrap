"""SQLite state store. All SQL is parameterized; timestamps are ISO-8601 UTC strings."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .enums import CompanyStatus
from .models import DiscoveredCompany
from .timeutil import iso, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,                 -- canonical domain, or 'name:<slug>' when unknown
    name TEXT NOT NULL,
    domain TEXT,
    website TEXT,
    careers_url TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    status_evidence TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ats_probed_at TEXT
);

CREATE TABLE IF NOT EXISTS company_sources (   -- discovery provenance
    company_id TEXT NOT NULL REFERENCES companies(id),
    source TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    batch TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (company_id, source, source_ref)
);

CREATE TABLE IF NOT EXISTS boards (           -- per-source scan checkpoints
    ats TEXT NOT NULL,
    board TEXT NOT NULL,
    company_id TEXT NOT NULL REFERENCES companies(id),
    enabled INTEGER NOT NULL DEFAULT 1,
    baseline_done INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,                 -- checkpoint: start time of last complete snapshot
    last_attempt_at TEXT,
    last_outcome TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    etag TEXT,
    last_modified TEXT,
    evidence TEXT,                        -- how the board identifier was resolved
    PRIMARY KEY (ats, board)
);

CREATE TABLE IF NOT EXISTS source_jobs (       -- latest raw (sanitized) payload per source job
    job_id TEXT PRIMARY KEY,
    ats TEXT NOT NULL,
    board TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (              -- normalized jobs
    job_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    company_name TEXT NOT NULL,
    ats TEXT NOT NULL,
    board TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    job_url TEXT,
    apply_url TEXT,
    location TEXT NOT NULL DEFAULT '',
    work_mode TEXT NOT NULL,
    eligibility TEXT NOT NULL,
    eligibility_reason TEXT,
    department TEXT,
    team TEXT,
    employment_type TEXT,
    seniority TEXT NOT NULL DEFAULT 'unknown',
    citizenship TEXT NOT NULL DEFAULT 'unknown',
    sponsorship TEXT NOT NULL DEFAULT 'unknown',
    source_published_at TEXT,
    source_updated_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    freshness_basis TEXT NOT NULL,
    is_new INTEGER NOT NULL DEFAULT 0,
    repost_of TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    missing_scans INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT,
    UNIQUE (ats, board, source_job_id)
);
CREATE INDEX IF NOT EXISTS ix_jobs_board_status ON jobs(ats, board, status);
CREATE INDEX IF NOT EXISTS ix_jobs_first_seen ON jobs(first_seen_at);
CREATE INDEX IF NOT EXISTS ix_jobs_eligibility ON jobs(eligibility, status);
CREATE INDEX IF NOT EXISTS ix_jobs_repost ON jobs(company_id, title, status);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,                 -- running | success | partial | failed
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_runs_kind_started ON runs(kind, started_at);

CREATE TABLE IF NOT EXISTS deliveries (        -- pending/complete row deliveries per target
    target TEXT NOT NULL,                 -- e.g. 'sheets:Jobs'
    row_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,                 -- pending | done
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (target, row_key)
);
CREATE INDEX IF NOT EXISTS ix_deliveries_status ON deliveries(target, status);

CREATE TABLE IF NOT EXISTS publish_state (
    target TEXT PRIMARY KEY,              -- 'csv' | 'sheets'
    last_success_at TEXT,                 -- window end of the last confirmed publication
    last_attempt_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-")


def canonical_domain(url_or_host: str | None) -> str | None:
    """Lower-cased registrable-ish host without scheme, port, 'www.' or trailing dot."""
    if not url_or_host:
        return None
    value = url_or_host.strip()
    if "://" not in value:
        value = "http://" + value
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    host = host.rstrip(".").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host


def company_id_for(name: str, domain: str | None) -> str:
    dom = canonical_domain(domain)
    return dom if dom else f"name:{slugify(name)}"


class LockHeld(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # DELETE journal keeps the DB a single self-contained file for state-branch persistence.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic write transaction (BEGIN IMMEDIATE serializes writers)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    # ---------- locks (prevent overlapping collectors on the same DB) ----------
    @contextmanager
    def lock(self, name: str, ttl_minutes: int = 120) -> Iterator[str]:
        owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        now = utcnow()
        with self.tx() as c:
            row = c.execute("SELECT owner, expires_at FROM locks WHERE name=?", (name,)).fetchone()
            if row and row["expires_at"] > iso(now):
                raise LockHeld(f"lock {name!r} held by {row['owner']} until {row['expires_at']}")
            c.execute(
                "INSERT OR REPLACE INTO locks(name, owner, expires_at) VALUES (?,?,?)",
                (name, owner, iso(now + timedelta(minutes=ttl_minutes))),
            )
            # We hold the lock, so any 'running' run of this kind was left by a crash.
            c.execute(
                "UPDATE runs SET status='interrupted', finished_at=? WHERE kind=? AND status='running'",
                (iso(now), name),
            )
        try:
            yield owner
        finally:
            with self.tx() as c:
                c.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner))

    # ---------- runs ----------
    def start_run(self, kind: str) -> str:
        run_id = f"{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{kind}-{uuid.uuid4().hex[:6]}"
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(id, kind, started_at, status) VALUES (?,?,?, 'running')",
                (run_id, kind, iso(utcnow())),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, stats: dict[str, Any], error: str | None = None):
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, stats=?, error=? WHERE id=?",
                (iso(utcnow()), status, json.dumps(stats, sort_keys=True), error, run_id),
            )

    def break_locks(self) -> int:
        """Operator escape hatch after a crash (locks also expire on their own)."""
        with self.tx() as c:
            return c.execute("DELETE FROM locks").rowcount

    # ---------- companies ----------
    def upsert_company(self, comp: DiscoveredCompany, now: datetime | None = None) -> str:
        now_s = iso(now or utcnow())
        cid = company_id_for(comp.name, comp.domain or comp.website)
        with self.tx() as c:
            existing = c.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
            if existing is None:
                c.execute(
                    """INSERT INTO companies(id, name, domain, website, careers_url, status,
                       status_evidence, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        cid,
                        comp.name,
                        canonical_domain(comp.domain or comp.website),
                        comp.website,
                        comp.careers_url,
                        comp.status.value,
                        comp.status_evidence,
                        now_s,
                        now_s,
                    ),
                )
            else:
                # Known status from evidence wins over unknown; never downgrade evidence to unknown.
                status = existing["status"]
                evidence = existing["status_evidence"]
                if comp.status != CompanyStatus.UNKNOWN:
                    status, evidence = comp.status.value, comp.status_evidence
                c.execute(
                    """UPDATE companies SET last_seen_at=?, status=?, status_evidence=?,
                       website=COALESCE(website, ?), careers_url=COALESCE(?, careers_url)
                       WHERE id=?""",
                    (now_s, status, evidence, comp.website, comp.careers_url, cid),
                )
            c.execute(
                """INSERT INTO company_sources(company_id, source, source_ref, batch,
                   first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(company_id, source, source_ref)
                   DO UPDATE SET last_seen_at=excluded.last_seen_at,
                                 batch=COALESCE(excluded.batch, batch)""",
                (cid, comp.source, comp.source_ref or "", comp.batch, now_s, now_s),
            )
            if comp.ats and comp.board:
                self._attach_board(c, cid, comp.ats.value, comp.board)
        return cid

    def _attach_board(
        self,
        c: sqlite3.Connection,
        company_id: str,
        ats: str,
        board: str,
        evidence: str = "discovery-source",
    ) -> None:
        c.execute(
            """INSERT INTO boards(ats, board, company_id, evidence) VALUES (?,?,?,?)
               ON CONFLICT(ats, board) DO NOTHING""",
            (ats, board.lower(), company_id, evidence),
        )

    def attach_board(self, company_id: str, ats: str, board: str, evidence: str) -> None:
        with self.tx() as c:
            self._attach_board(c, company_id, ats, board, evidence)

    def mark_probed(self, company_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE companies SET ats_probed_at=? WHERE id=?", (iso(utcnow()), company_id)
            )

    def companies_needing_probe(self, limit: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT c.* FROM companies c
               WHERE c.ats_probed_at IS NULL
                 AND c.status NOT IN ('inactive','acquired','public')
                 AND NOT EXISTS (SELECT 1 FROM boards b WHERE b.company_id=c.id)
               ORDER BY c.first_seen_at LIMIT ?""",
            (limit,),
        )

    def boards_to_scan(self, limit: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT b.*, c.name AS company_name, c.status AS company_status
               FROM boards b JOIN companies c ON c.id=b.company_id
               WHERE b.enabled=1 AND c.status NOT IN ('inactive','acquired','public')
               ORDER BY COALESCE(b.last_attempt_at, '') ASC LIMIT ?""",
            (limit,),
        )

    # ---------- retention ----------
    def prune(self, retention_days: int) -> int:
        cutoff = iso(utcnow() - timedelta(days=retention_days))
        with self.tx() as c:
            n = c.execute(
                "DELETE FROM source_jobs WHERE job_id IN "
                "(SELECT job_id FROM jobs WHERE status='closed' AND closed_at < ?)",
                (cutoff,),
            ).rowcount
            n += c.execute(
                "DELETE FROM jobs WHERE status='closed' AND closed_at < ?", (cutoff,)
            ).rowcount
            c.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
        return n

    def snapshot_to(self, dest: str | Path) -> None:
        """Consistent single-file copy (safe even while open)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        self.conn.execute("VACUUM INTO ?", (str(dest),))
