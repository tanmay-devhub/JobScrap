"""Apply one board scan to the store: baseline, freshness labels, reposts, removal inference.

Invariants:
* Only a *complete* snapshot (SUCCESS / ZERO_JOBS, incl. 304 not-modified) advances the board
  checkpoint, updates last_seen_at, or counts toward closing missing jobs.
* Failed / partial scans change nothing except failure bookkeeping on the board row.
* The first complete scan of a board is a baseline: its undated listings are not "new";
  explicitly dated listings published within `baseline_recent_days` are new (source_published).
* `source_published_at` keeps the earliest value ever observed: a republish never resets it.
* A job is closed only after `close_after_missing_scans` consecutive complete snapshots
  without it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .adapters.base import BoardSnapshot
from .config import AppConfig
from .db import Store
from .enums import FreshnessBasis, JobStatus, ScanOutcome
from .models import RawJob, job_identity
from .normalize import citizenship, classify, seniority, sponsorship
from .timeutil import iso, parse_iso


@dataclass
class ApplyResult:
    outcome: ScanOutcome
    fetched: int = 0
    new: int = 0
    baseline: int = 0
    reopened: int = 0
    closed: int = 0
    reposts: int = 0
    non_us: int = 0
    unclear: int = 0
    checkpoint_advanced: bool = False
    errors: list[str] = field(default_factory=list)


def _find_repost(
    c: sqlite3.Connection, company_id: str, job: RawJob, location: str, job_id: str, since: str
) -> str | None:
    """Closed job, same company + exact title + location, closed within the repost window."""
    row = c.execute(
        """SELECT job_id FROM jobs WHERE company_id=? AND lower(title)=? AND location=?
           AND status='closed' AND closed_at>=? AND job_id<>? ORDER BY closed_at DESC LIMIT 1""",
        (company_id, job.title[:500].lower(), location, since, job_id),
    ).fetchone()
    return row["job_id"] if row else None


def apply_snapshot(
    store: Store,
    board_row: sqlite3.Row | dict,
    snap: BoardSnapshot,
    scan_started: datetime,
    cfg: AppConfig,
) -> ApplyResult:
    ats, board, company_id = board_row["ats"], board_row["board"], board_row["company_id"]
    started = iso(scan_started)
    result = ApplyResult(outcome=snap.outcome, fetched=len(snap.jobs))

    if not snap.complete:
        with store.tx() as c:
            not_found = snap.outcome == ScanOutcome.NOT_FOUND
            c.execute(
                """UPDATE boards SET last_attempt_at=?, last_outcome=?, last_error=?,
                   consecutive_failures=consecutive_failures+1 WHERE ats=? AND board=?""",
                (started, snap.outcome.value, (snap.error or "")[:500], ats, board),
            )
            row = c.execute(
                "SELECT consecutive_failures FROM boards WHERE ats=? AND board=?", (ats, board)
            ).fetchone()
            if (
                not_found
                and row
                and row["consecutive_failures"] >= cfg.freshness.disable_board_after_not_found
            ):
                c.execute("UPDATE boards SET enabled=0 WHERE ats=? AND board=?", (ats, board))
        return result

    fr = cfg.freshness
    with store.tx() as c:
        b = c.execute("SELECT * FROM boards WHERE ats=? AND board=?", (ats, board)).fetchone()
        company = c.execute("SELECT name FROM companies WHERE id=?", (company_id,)).fetchone()
        company_name = company["name"] if company else company_id
        is_baseline = not b["baseline_done"]
        checkpoint = parse_iso(b["last_success_at"])
        fresh_floor = (
            scan_started - timedelta(days=fr.baseline_recent_days)
            if is_baseline or checkpoint is None
            else checkpoint - timedelta(hours=fr.checkpoint_overlap_hours)
        )
        repost_since = iso(scan_started - timedelta(days=fr.repost_window_days))

        if snap.not_modified:
            # Board unchanged since last complete scan: everything open is still present.
            c.execute(
                """UPDATE jobs SET last_seen_at=?, missing_scans=0
                   WHERE ats=? AND board=? AND status='open'""",
                (started, ats, board),
            )
        else:
            seen_ids: set[str] = set()
            for job in snap.jobs:
                jid = job_identity(ats, board, job.source_job_id)
                if jid in seen_ids:
                    continue  # duplicate within one payload
                seen_ids.add(jid)
                _upsert_job(
                    c,
                    result,
                    jid,
                    ats,
                    board,
                    company_id,
                    company_name,
                    job,
                    started,
                    is_baseline,
                    fresh_floor,
                    repost_since,
                    cfg,
                )

            # Removal inference over open jobs of this board that were not in the snapshot.
            open_rows = c.execute(
                "SELECT job_id, missing_scans FROM jobs WHERE ats=? AND board=? AND status='open'",
                (ats, board),
            ).fetchall()
            for r in open_rows:
                if r["job_id"] in seen_ids:
                    continue
                missing = r["missing_scans"] + 1
                if missing >= fr.close_after_missing_scans:
                    c.execute(
                        "UPDATE jobs SET status='closed', closed_at=?, missing_scans=? WHERE job_id=?",
                        (started, missing, r["job_id"]),
                    )
                    result.closed += 1
                else:
                    c.execute(
                        "UPDATE jobs SET missing_scans=? WHERE job_id=?", (missing, r["job_id"])
                    )

        c.execute(
            """UPDATE boards SET baseline_done=1, last_success_at=?, last_attempt_at=?,
               last_outcome=?, last_error=NULL, consecutive_failures=0,
               etag=COALESCE(?, etag) WHERE ats=? AND board=?""",
            (started, started, snap.outcome.value, snap.etag, ats, board),
        )
        result.checkpoint_advanced = True
    return result


def reclassify(store: Store, cfg: AppConfig) -> dict[str, int]:
    """Re-run eligibility/work-mode rules over stored payloads (no network). Use after
    classifier changes: boards answering 304 never resend their jobs."""
    changed = 0
    rows = store.q(
        "SELECT j.job_id, j.eligibility, j.work_mode, s.payload FROM jobs j "
        "JOIN source_jobs s ON s.job_id=j.job_id"
    )
    with store.tx() as c:
        for r in rows:
            cls = classify(
                RawJob.model_validate_json(r["payload"]),
                worldwide_counts_as_us=cfg.filters.worldwide_remote_counts_as_us,
            )
            if (cls.eligibility.value, cls.work_mode.value) != (r["eligibility"], r["work_mode"]):
                changed += 1
            c.execute(
                "UPDATE jobs SET eligibility=?, work_mode=?, eligibility_reason=? WHERE job_id=?",
                (cls.eligibility.value, cls.work_mode.value, cls.reason, r["job_id"]),
            )
    return {"jobs": len(rows), "changed": changed}


def _upsert_job(
    c,
    result: ApplyResult,
    jid,
    ats,
    board,
    company_id,
    company_name,
    job: RawJob,
    started: str,
    is_baseline: bool,
    fresh_floor: datetime,
    repost_since: str,
    cfg: AppConfig,
) -> None:
    cls = classify(job, worldwide_counts_as_us=cfg.filters.worldwide_remote_counts_as_us)
    if cls.eligibility == "non_us":
        result.non_us += 1
    elif cls.eligibility == "location_unclear":
        result.unclear += 1
    location = " | ".join([job.location, *job.extra_locations]).strip(" |")[:1000]
    published = iso(job.published_at)
    updated = iso(job.updated_at)
    payload = job.model_dump(mode="json", exclude={"description_text"})
    c.execute(
        """INSERT INTO source_jobs(job_id, ats, board, source_job_id, payload, fetched_at)
           VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload,
           fetched_at=excluded.fetched_at""",
        (jid, ats, board, job.source_job_id, json.dumps(payload, sort_keys=True), started),
    )
    common = dict(
        title=job.title[:500],
        job_url=job.job_url,
        apply_url=job.apply_url,
        location=location,
        work_mode=cls.work_mode.value,
        eligibility=cls.eligibility.value,
        eligibility_reason=cls.reason,
        department=job.department,
        team=job.team,
        employment_type=job.employment_type,
        seniority=seniority(job.title),
        citizenship=citizenship(job.description_text),
        sponsorship=sponsorship(job.description_text),
        updated=updated,
        started=started,
        jid=jid,
    )
    existing = c.execute(
        "SELECT status, source_published_at FROM jobs WHERE job_id=?", (jid,)
    ).fetchone()
    if existing is not None:
        # Keep earliest publication ever observed; republish must not look like creation.
        pub = existing["source_published_at"]
        if published and (pub is None or published < pub):
            pub = published
        if existing["status"] == JobStatus.CLOSED:
            result.reopened += 1
        c.execute(
            """UPDATE jobs SET title=:title, job_url=:job_url, apply_url=:apply_url,
               location=:location, work_mode=:work_mode, eligibility=:eligibility,
               eligibility_reason=:eligibility_reason, department=:department, team=:team,
               employment_type=:employment_type, seniority=:seniority,
               citizenship=CASE WHEN :citizenship<>'unknown' THEN :citizenship ELSE citizenship END,
               sponsorship=CASE WHEN :sponsorship<>'unknown' THEN :sponsorship ELSE sponsorship END,
               source_published_at=:pub, source_updated_at=:updated, last_seen_at=:started,
               status='open', missing_scans=0, closed_at=NULL WHERE job_id=:jid""",
            {**common, "pub": pub},
        )
        return

    recent = job.published_at is not None and job.published_at >= fresh_floor
    repost_of = None
    if is_baseline:
        basis = FreshnessBasis.SOURCE_PUBLISHED if recent else FreshnessBasis.BASELINE
        is_new = recent
    else:
        basis = FreshnessBasis.SOURCE_PUBLISHED if recent else FreshnessBasis.NEWLY_DETECTED
        is_new = True
        repost_of = _find_repost(c, company_id, job, location, jid, repost_since)
        if repost_of:
            is_new = cfg.freshness.reposts_count_as_new
            result.reposts += 1
    if is_new:
        result.new += 1
    else:
        result.baseline += 1 if repost_of is None else 0
    c.execute(
        """INSERT INTO jobs(job_id, company_id, company_name, ats, board, source_job_id, title,
           job_url, apply_url, location, work_mode, eligibility, eligibility_reason, department,
           team, employment_type, seniority, citizenship, sponsorship, source_published_at,
           source_updated_at, first_seen_at, last_seen_at, freshness_basis, is_new, repost_of,
           status) VALUES (:jid, :company_id, :company_name, :ats, :board, :sjid, :title,
           :job_url, :apply_url, :location, :work_mode, :eligibility, :eligibility_reason,
           :department, :team, :employment_type, :seniority, :citizenship, :sponsorship,
           :pub, :updated, :started, :started, :basis, :is_new, :repost_of, 'open')""",
        {
            **common,
            "company_id": company_id,
            "company_name": company_name,
            "ats": ats,
            "board": board,
            "sjid": job.source_job_id,
            "pub": published,
            "basis": basis.value,
            "is_new": int(is_new),
            "repost_of": repost_of,
        },
    )
