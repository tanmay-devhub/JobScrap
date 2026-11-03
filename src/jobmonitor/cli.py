"""Command-line interface.

Exit codes: 0 success/partial, 2 failed, 3 another run holds the lock, 4 credential error,
5 state conflict.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .config import EnvSettings, load_config
from .db import LockHeld, Store
from .enums import ATSKind
from .models import DiscoveredCompany
from .security import setup_logging

log = logging.getLogger("jobmonitor")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jobmonitor", description="Startup job monitor (US-eligible roles)."
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "--config", help="YAML config path (env JOBMONITOR_CONFIG_PATH, default config.yaml)"
    )
    p.add_argument("--db", help="SQLite path (env JOBMONITOR_DB_PATH)")
    p.add_argument("--log-level", help="DEBUG/INFO/WARNING")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="discover companies (weekly) and resolve ATS boards")
    d.add_argument("--source", action="append", choices=["yc", "techstars", "a16z"])
    d.add_argument("--no-probe", action="store_true", help="skip board resolution probes")

    c = sub.add_parser("collect", help="scan known ATS boards (daily)")
    c.add_argument("--limit", type=int, help="max boards this run")

    pub = sub.add_parser("publish", help="write CSV (and Sheets if enabled) when due")
    pub.add_argument("--force", action="store_true", help="publish even if not due")
    g = pub.add_mutually_exclusive_group()
    g.add_argument("--sheets", dest="sheets", action="store_true", default=None)
    g.add_argument("--no-sheets", dest="sheets", action="store_false")

    r = sub.add_parser("run", help="end-to-end: discover if due, collect, publish if due")
    r.add_argument("--force-discover", action="store_true")
    r.add_argument("--force-publish", action="store_true")
    r.add_argument("--skip-discover", action="store_true")

    sub.add_parser("status", help="show boards, jobs, and recent runs")

    a = sub.add_parser("add-company", help="add/curate a company and optionally its ATS board")
    a.add_argument("--name", required=True)
    a.add_argument("--website")
    a.add_argument("--ats", choices=[k.value for k in ATSKind])
    a.add_argument("--board")

    s = sub.add_parser("state", help="restore/save SQLite state on a git branch (CI)")
    s.add_argument("action", choices=["restore", "save"])
    s.add_argument("--repo", default=".")
    s.add_argument("--branch", default="state")
    s.add_argument("--remote", default="origin")
    s.add_argument("--run-label", default="")

    demo = sub.add_parser("demo", help="offline, credential-free demo using synthetic data")
    demo.add_argument("--out", default="demo-output")

    sub.add_parser(
        "reclassify", help="re-apply location/eligibility rules to stored jobs (offline)"
    )
    sub.add_parser("unlock", help="clear stale run locks after a crash")
    sub.add_parser("resync-sheets", help="forget Sheets delivery state so every row is re-sent")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = EnvSettings()
    if args.config:
        env.config_path = args.config
    if args.db:
        env.db_path = args.db
    setup_logging(args.log_level or env.log_level)

    if args.cmd == "demo":
        from .demo import run_demo

        res = run_demo(args.out)
        print(
            json.dumps(
                {
                    k: {
                        kk: v.get(kk)
                        for kk in (
                            "status",
                            "jobs_new",
                            "jobs_baseline",
                            "jobs_closed",
                            "jobs_unclear",
                            "publication",
                        )
                    }
                    for k, v in res.items()
                },
                indent=2,
            )
        )
        print(f"CSV exports: {Path(args.out, 'exports').resolve()}")
        return 0

    cfg = load_config(env.config_path)
    if env.sheets_enabled is not None:
        cfg.publish.sheets_enabled = env.sheets_enabled
    if args.cmd == "state":
        from .statesync import GitState, StateConflict, StateError

        gs = GitState(Path(args.repo), args.branch, args.remote)
        try:
            if args.action == "restore":
                gs.restore(env.db_path)
            else:
                store = Store(env.db_path)
                gs.save(store, args.run_label)
                store.close()
        except StateConflict as e:
            log.error("%s", e)
            return 5
        except StateError as e:
            log.error("%s", e)
            return 2
        return 0

    from .pipeline import Context, collect, discover, publish, run_all

    store = Store(env.db_path)
    ctx = Context(cfg, env, store)
    try:
        if args.cmd == "discover":
            res = discover(ctx, args.source, probe=False if args.no_probe else None)
        elif args.cmd == "collect":
            res = collect(ctx, args.limit)
        elif args.cmd == "publish":
            res = publish(ctx, force=args.force, sheets=args.sheets)
        elif args.cmd == "run":
            res = run_all(ctx, args.force_discover, args.force_publish, args.skip_discover)
        elif args.cmd == "status":
            return _status(store)
        elif args.cmd == "add-company":
            if bool(args.ats) != bool(args.board):
                print("--ats and --board must be given together", file=sys.stderr)
                return 2
            cid = store.upsert_company(
                DiscoveredCompany(
                    name=args.name,
                    website=args.website,
                    domain=args.website,
                    ats=ATSKind(args.ats) if args.ats else None,
                    board=args.board.lower() if args.board else None,
                    source="manual",
                )
            )
            print(cid)
            return 0
        elif args.cmd == "reclassify":
            from .freshness import reclassify

            print(json.dumps(reclassify(store, cfg)))
            return 0
        elif args.cmd == "unlock":
            print(f"cleared {store.break_locks()} lock(s)")
            return 0
        elif args.cmd == "resync-sheets":
            with store.tx() as c:
                n = c.execute("DELETE FROM deliveries WHERE target LIKE 'sheets:%'").rowcount
            print(f"cleared {n} delivery record(s)")
            return 0
        else:  # pragma: no cover
            return 2
    except LockHeld as e:
        log.error("%s", e)
        return 3
    finally:
        store.close()
    print(json.dumps(res, indent=2, default=str))
    return _exit_code(res)


def _exit_code(res) -> int:
    runs = [res] if "status" in res else [v for v in res.values() if isinstance(v, dict)]
    if any("credential" in str(r.get("publication", "")) for r in runs):
        return 4
    return 2 if any(r.get("status") == "failed" for r in runs) else 0


def _status(store: Store) -> int:
    def show(title, rows):
        print(f"\n== {title}")
        for r in rows:
            print("  " + "  ".join(f"{k}={r[k]}" for k in r.keys()))

    show(
        "boards",
        store.q(
            "SELECT ats, board, enabled, baseline_done, last_outcome, last_success_at, "
            "consecutive_failures FROM boards ORDER BY ats, board LIMIT 50"
        ),
    )
    show(
        "jobs by eligibility",
        store.q(
            "SELECT eligibility, status, count(*) AS n FROM jobs "
            "GROUP BY eligibility, status ORDER BY n DESC"
        ),
    )
    show(
        "recent runs",
        store.q("SELECT id, status, finished_at FROM runs ORDER BY started_at DESC LIMIT 10"),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
