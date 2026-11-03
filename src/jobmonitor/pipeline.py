"""Orchestration: discover (weekly), collect (daily), publish (every N days), run (all, due-aware)."""

from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .adapters import ADAPTERS
from .config import AppConfig, EnvSettings
from .db import Store
from .discovery import SOURCES
from .discovery.resolve import resolve_company
from .enums import ATSKind, ScanOutcome
from .exports import csv_export, views
from .freshness import apply_snapshot
from .http_client import HttpClient
from .models import DiscoveredCompany
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)
DUE_TOLERANCE = timedelta(hours=6)  # schedules drift; don't skip a cycle over a late start


@dataclass
class Context:
    cfg: AppConfig
    env: EnvSettings
    store: Store
    client_factory: callable = None  # returns HttpClient; injectable for tests
    now: callable = utcnow
    stats: dict = field(default_factory=dict)

    def client(self) -> HttpClient:
        return self.client_factory() if self.client_factory else HttpClient(self.cfg.http)


def _finish(
    ctx: Context,
    run_id: str,
    kind: str,
    stats: dict,
    t0: float,
    attempted: int,
    failed: int,
    error: str | None = None,
) -> dict:
    stats["duration_s"] = round(time.monotonic() - t0, 1)
    if error or attempted and failed == attempted:
        status = "failed"
    elif failed:
        status = "partial"
    else:
        status = "success"
    ctx.store.finish_run(run_id, status, stats, error)
    stats["status"] = status
    stats["run_id"] = run_id
    log.info("%s %s: %s", kind, status, {k: v for k, v in stats.items() if k != "outcomes"})
    return stats


def last_success(store: Store, kind: str) -> datetime | None:
    row = store.one(
        "SELECT max(started_at) AS t FROM runs WHERE kind=? AND status IN ('success','partial')",
        (kind,),
    )
    return parse_iso(row["t"]) if row and row["t"] else None


# ---------------- discover ----------------
def discover(ctx: Context, sources: list[str] | None = None, probe: bool | None = None) -> dict:
    cfg, store = ctx.cfg, ctx.store
    t0 = time.monotonic()
    stats: dict = {
        "sources_attempted": 0,
        "sources_succeeded": 0,
        "sources_failed": 0,
        "companies_seen": 0,
        "boards_resolved": 0,
        "probes": 0,
        "notes": {},
    }
    with store.lock("discover"), ctx.client() as client:
        run_id = store.start_run("discover")
        for seed in cfg.seed_companies:
            comp = DiscoveredCompany(source="seed", **seed)
            store.upsert_company(comp, ctx.now())
        for name in sources or cfg.discovery.sources:
            src = SOURCES.get(name)
            if src is None:
                stats["notes"][name] = "unknown source"
                continue
            stats["sources_attempted"] += 1
            res = src.discover(client, cfg.discovery)
            if not res.ok:
                stats["sources_failed"] += 1
                stats["notes"][name] = res.error
                log.warning("discovery %s failed: %s", name, res.error)
                continue
            stats["sources_succeeded"] += 1
            if res.note:
                stats["notes"][name] = res.note
            for comp in res.companies:
                store.upsert_company(comp, ctx.now())
            stats["companies_seen"] += len(res.companies)

        if probe if probe is not None else cfg.discovery.probe_ats:
            pending = store.companies_needing_probe(cfg.discovery.max_probes_per_run)
            with ThreadPoolExecutor(max_workers=cfg.http.max_concurrency) as pool:
                futs = {
                    pool.submit(resolve_company, client, r["name"], r["website"]): r
                    for r in pending
                }
                for fut in as_completed(futs):
                    row = futs[fut]
                    stats["probes"] += 1
                    try:
                        found = fut.result()
                    except Exception as e:  # noqa: BLE001 - isolate per-company failures
                        log.warning("probe %s failed: %s", row["id"], type(e).__name__)
                        continue  # not marked probed -> retried next run
                    if found:
                        kind, board, evidence = found
                        store.attach_board(row["id"], kind.value, board, evidence)
                        stats["boards_resolved"] += 1
                    store.mark_probed(row["id"])
        stats["requests"] = client.stats.requests
    return _finish(
        ctx, run_id, "discover", stats, t0, stats["sources_attempted"], stats["sources_failed"]
    )


# ---------------- collect ----------------
def collect(ctx: Context, limit: int | None = None) -> dict:
    cfg, store = ctx.cfg, ctx.store
    t0 = time.monotonic()
    outcomes: Counter = Counter()
    stats: dict = {
        "sources_attempted": 0,
        "sources_succeeded": 0,
        "sources_failed": 0,
        "jobs_fetched": 0,
        "jobs_new": 0,
        "jobs_baseline": 0,
        "jobs_closed": 0,
        "jobs_reposts": 0,
        "jobs_filtered": 0,
        "jobs_unclear": 0,
        "failures": [],
    }
    with store.lock("collect"), ctx.client() as client:
        run_id = store.start_run("collect")
        boards = store.boards_to_scan(limit or cfg.collect.max_boards_per_run)

        def scan(b):
            started = ctx.now()
            adapter = ADAPTERS[ATSKind(b["ats"])]
            return (
                b,
                started,
                adapter.scan(client, b["board"], b["etag"], cfg.collect.fetch_descriptions),
            )

        with ThreadPoolExecutor(max_workers=cfg.http.max_concurrency) as pool:
            futures = [pool.submit(scan, dict(b)) for b in boards]
            for fut in as_completed(futures):
                b, started, snap = fut.result()  # scan() never raises for source problems
                stats["sources_attempted"] += 1
                outcomes[snap.outcome.value] += 1
                res = apply_snapshot(store, b, snap, started, cfg)  # single writer thread
                if res.checkpoint_advanced:
                    stats["sources_succeeded"] += 1
                else:
                    stats["sources_failed"] += 1
                    stats["failures"].append(f"{b['ats']}:{b['board']}={snap.outcome.value}")
                stats["jobs_fetched"] += res.fetched
                stats["jobs_new"] += res.new
                stats["jobs_baseline"] += res.baseline
                stats["jobs_closed"] += res.closed
                stats["jobs_reposts"] += res.reposts
                stats["jobs_filtered"] += res.non_us
                stats["jobs_unclear"] += res.unclear
        stats["requests"] = client.stats.requests
        stats["retries"] = client.stats.retries
        stats["not_modified"] = client.stats.not_modified
    stats["outcomes"] = dict(outcomes)
    stats["failures"] = stats["failures"][:50]
    store.prune(cfg.freshness.retention_days)
    # NOT_FOUND on a deleted board is a real answer, not an outage: do not fail the whole run on it.
    hard_failed = sum(
        v
        for k, v in outcomes.items()
        if k != ScanOutcome.NOT_FOUND.value
        and k not in (ScanOutcome.SUCCESS.value, ScanOutcome.ZERO_JOBS.value)
    )
    return _finish(ctx, run_id, "collect", stats, t0, stats["sources_attempted"], hard_failed)


# ---------------- publish ----------------
def publish_due(store: Store, target: str, every_days: int, now: datetime) -> bool:
    row = store.one("SELECT last_success_at FROM publish_state WHERE target=?", (target,))
    last = parse_iso(row["last_success_at"]) if row else None
    return last is None or now - last >= timedelta(days=every_days) - DUE_TOLERANCE


def _window(store: Store, target: str, every_days: int, now: datetime) -> tuple[datetime, datetime]:
    row = store.one("SELECT last_success_at FROM publish_state WHERE target=?", (target,))
    last = parse_iso(row["last_success_at"]) if row else None
    return (last or now - timedelta(days=every_days)), now


def _mark_published(store: Store, target: str, until: datetime | None, error: str | None) -> None:
    with store.tx() as c:
        c.execute(
            """INSERT INTO publish_state(target, last_success_at, last_attempt_at, last_error)
               VALUES (?,?,?,?) ON CONFLICT(target) DO UPDATE SET
               last_success_at=COALESCE(excluded.last_success_at, publish_state.last_success_at),
               last_attempt_at=excluded.last_attempt_at, last_error=excluded.last_error""",
            (target, iso(until) if until else None, iso(utcnow()), error),
        )


def build_views(ctx: Context, since: datetime, until: datetime) -> list[views.View]:
    f = ctx.cfg.filters
    return [
        views.companies_view(ctx.store),
        views.jobs_view(ctx.store, f, until),
        views.new_jobs_view(ctx.store, f, since, until),
        views.unclear_view(ctx.store, f),
        views.runs_view(ctx.store),
    ]


def publish(ctx: Context, force: bool = False, sheets: bool | None = None, backend=None) -> dict:
    """CSV always; Sheets when enabled. Window = since last *confirmed* publish -> no gaps."""
    cfg, store, env = ctx.cfg, ctx.store, ctx.env
    t0 = time.monotonic()
    now = ctx.now()
    stats: dict = {"sources_attempted": 0, "sources_failed": 0, "publication": ""}
    results = []
    error = None
    with store.lock("publish"):
        run_id = store.start_run("publish")
        # CSV
        if force or publish_due(store, "csv", cfg.publish.every_days, now):
            stats["sources_attempted"] += 1
            since, until = _window(store, "csv", cfg.publish.every_days, now)
            for v in build_views(ctx, since, until):
                csv_export.write_csv(v, cfg.publish.csv_dir)
                stats[f"csv_{v.name}"] = len(v.rows)
            _mark_published(store, "csv", until, None)
            results.append("csv:ok")
        else:
            results.append("csv:not-due")

        want_sheets = cfg.publish.sheets_enabled if sheets is None else sheets
        if want_sheets and (force or publish_due(store, "sheets", cfg.publish.every_days, now)):
            stats["sources_attempted"] += 1
            from .exports.sheets import (
                CredentialError,
                GoogleSheetsBackend,
                SheetsError,
                publish_views,
            )

            since, until = _window(store, "sheets", cfg.publish.every_days, now)
            try:
                if backend is None:
                    if not env.sheets_spreadsheet_id or not env.google_service_account_json:
                        raise CredentialError(
                            "Sheets enabled but JOBMONITOR_SHEETS_SPREADSHEET_ID / "
                            "JOBMONITOR_GOOGLE_SERVICE_ACCOUNT_JSON not set"
                        )
                    backend = GoogleSheetsBackend(
                        env.sheets_spreadsheet_id, env.google_service_account_json
                    )
                written = publish_views(store, backend, build_views(ctx, since, until))
                stats["sheets_rows_written"] = written
                _mark_published(store, "sheets", until, None)
                results.append("sheets:ok")
            except CredentialError as e:
                stats["sources_failed"] += 1
                _mark_published(store, "sheets", None, f"credential: {e}")
                results.append("sheets:credential-error")
                error = f"sheets credential error: {e}"
            except SheetsError as e:
                stats["sources_failed"] += 1
                _mark_published(store, "sheets", None, f"source: {e}")
                results.append("sheets:error")
                error = f"sheets error: {e}"
        elif want_sheets:
            results.append("sheets:not-due")
    stats["publication"] = ", ".join(results)
    return _finish(
        ctx,
        run_id,
        "publish",
        stats,
        t0,
        stats["sources_attempted"],
        stats["sources_failed"],
        error,
    )


# ---------------- run ----------------
def discovery_due(store: Store, now: datetime, days: int = 7) -> bool:
    last = last_success(store, "discover")
    return last is None or now - last >= timedelta(days=days) - DUE_TOLERANCE


def run_all(ctx: Context, force_discover=False, force_publish=False, skip_discover=False) -> dict:
    store = ctx.store
    out: dict = {}
    if not skip_discover and (force_discover or discovery_due(store, ctx.now())):
        out["discover"] = discover(ctx)
    else:
        out["discover"] = "not-due"
    out["collect"] = collect(ctx)
    out["publish"] = publish(ctx, force=force_publish)
    return out
