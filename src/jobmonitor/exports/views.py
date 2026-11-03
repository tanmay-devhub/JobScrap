"""Tabular views shared by CSV and Sheets. Every cell is sanitized here, once."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import FilterConfig
from ..db import Store
from ..security import neutralize_cell, safe_link
from ..timeutil import iso, pacific_display

ELIGIBLE = ("us", "remote_us_ok")
RECENTLY_CLOSED_DAYS = 30


@dataclass
class View:
    name: str
    header: list[str]
    rows: list[list[str]]  # first column is the stable row key


JOB_HEADER = [
    "job_id",
    "company",
    "title",
    "location",
    "work_mode",
    "us_eligibility",
    "eligibility_reason",
    "department",
    "seniority",
    "sponsorship",
    "citizenship",
    "source",
    "source_job_id",
    "job_url",
    "apply_url",
    "source_published_pt",
    "source_updated_pt",
    "first_seen_pt",
    "last_seen_pt",
    "freshness_basis",
    "is_new",
    "repost_of",
    "status",
]


def passes_filters(row, f: FilterConfig) -> bool:
    """Optional filters. Unknown values never cause exclusion."""
    title = row["title"] or ""
    if f.title_include_regex and not re.search(f.title_include_regex, title, re.I):
        return False
    if f.title_exclude_regex and re.search(f.title_exclude_regex, title, re.I):
        return False
    dept = (row["department"] or "").lower()
    if dept and any(d.lower() == dept for d in f.departments_exclude):
        return False
    if row["seniority"] != "unknown" and row["seniority"] in f.seniority_exclude:
        return False
    if f.exclude_citizenship_required and row["citizenship"] == "required":
        return False
    return not (f.exclude_no_sponsorship and row["sponsorship"] == "not_offered")


def _job_row(r) -> list[str]:
    vals = [
        r["job_id"],
        r["company_name"],
        r["title"],
        r["location"],
        r["work_mode"],
        r["eligibility"],
        r["eligibility_reason"],
        r["department"],
        r["seniority"],
        r["sponsorship"],
        r["citizenship"],
        r["ats"],
        r["source_job_id"],
    ]
    cells = [neutralize_cell(v) for v in vals]
    cells += [safe_link(r["job_url"]), safe_link(r["apply_url"])]
    cells += [
        neutralize_cell(v)
        for v in (
            pacific_display(r["source_published_at"]),
            pacific_display(r["source_updated_at"]),
            pacific_display(r["first_seen_at"]),
            pacific_display(r["last_seen_at"]),
            r["freshness_basis"],
            "yes" if r["is_new"] else "no",
            r["repost_of"],
            r["status"],
        )
    ]
    return cells


def jobs_view(store: Store, f: FilterConfig, now: datetime) -> View:
    cutoff = iso(now - timedelta(days=RECENTLY_CLOSED_DAYS))
    rows = store.q(
        f"""SELECT * FROM jobs WHERE eligibility IN ({",".join("?" * len(ELIGIBLE))})
            AND (status='open' OR closed_at>=?) ORDER BY first_seen_at DESC, company_name, title""",
        (*ELIGIBLE, cutoff),
    )
    return View("Jobs", JOB_HEADER, [_job_row(r) for r in rows if passes_filters(r, f)])


def new_jobs_view(store: Store, f: FilterConfig, since: datetime, until: datetime) -> View:
    rows = store.q(
        f"""SELECT * FROM jobs WHERE is_new=1 AND eligibility IN ({",".join("?" * len(ELIGIBLE))})
            AND first_seen_at>=? AND first_seen_at<=? ORDER BY first_seen_at DESC, company_name""",
        (*ELIGIBLE, iso(since), iso(until)),
    )
    window = f"{pacific_display(since)} -> {pacific_display(until)}"
    header = [*JOB_HEADER, "publish_window"]
    return View(
        "NewJobs",
        header,
        [[*_job_row(r), neutralize_cell(window)] for r in rows if passes_filters(r, f)],
    )


def unclear_view(store: Store, f: FilterConfig) -> View:
    rows = store.q(
        "SELECT * FROM jobs WHERE eligibility='location_unclear' AND status='open' "
        "ORDER BY first_seen_at DESC, company_name"
    )
    return View("LocationUnclear", JOB_HEADER, [_job_row(r) for r in rows if passes_filters(r, f)])


COMPANY_HEADER = [
    "company_id",
    "name",
    "domain",
    "website",
    "status",
    "status_evidence",
    "discovered_via",
    "boards",
    "first_seen_pt",
    "last_seen_pt",
]


def companies_view(store: Store) -> View:
    rows = store.q(
        """SELECT c.*,
             (SELECT group_concat(source || COALESCE(':' || NULLIF(batch, ''), ''), ', ')
                FROM company_sources s WHERE s.company_id=c.id) AS via,
             (SELECT group_concat(ats || ':' || board, ', ') FROM boards b
                WHERE b.company_id=c.id AND b.enabled=1) AS boards
           FROM companies c ORDER BY c.name"""
    )
    out = []
    for r in rows:
        out.append(
            [
                neutralize_cell(r["id"]),
                neutralize_cell(r["name"]),
                neutralize_cell(r["domain"]),
                safe_link(r["website"]),
                neutralize_cell(r["status"]),
                neutralize_cell(r["status_evidence"]),
                neutralize_cell(r["via"]),
                neutralize_cell(r["boards"]),
                neutralize_cell(pacific_display(r["first_seen_at"])),
                neutralize_cell(pacific_display(r["last_seen_at"])),
            ]
        )
    return View("Companies", COMPANY_HEADER, out)


RUN_HEADER = [
    "run_id",
    "kind",
    "status",
    "started_pt",
    "finished_pt",
    "duration_s",
    "sources_attempted",
    "sources_succeeded",
    "sources_failed",
    "jobs_fetched",
    "jobs_new",
    "jobs_filtered",
    "requests",
    "publication",
    "error",
]


def runs_view(store: Store, limit: int = 500) -> View:
    rows = store.q("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        s = json.loads(r["stats"] or "{}")
        dur = s.get("duration_s", "")
        out.append(
            [
                neutralize_cell(v)
                for v in (
                    r["id"],
                    r["kind"],
                    r["status"],
                    pacific_display(r["started_at"]),
                    pacific_display(r["finished_at"]),
                    dur,
                    s.get("sources_attempted", ""),
                    s.get("sources_succeeded", ""),
                    s.get("sources_failed", ""),
                    s.get("jobs_fetched", ""),
                    s.get("jobs_new", ""),
                    s.get("jobs_filtered", ""),
                    s.get("requests", ""),
                    s.get("publication", ""),
                    r["error"],
                )
            ]
        )
    return View("Runs", RUN_HEADER, out)
