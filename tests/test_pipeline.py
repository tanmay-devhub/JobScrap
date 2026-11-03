from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from conftest import T0, add_board, fast_http
from jobmonitor import cli
from jobmonitor.config import EnvSettings
from jobmonitor.demo import mock_transport
from jobmonitor.enums import ATSKind
from jobmonitor.http_client import HttpClient
from jobmonitor.pipeline import Context, collect, publish, publish_due
from jobmonitor.timeutil import PACIFIC, pacific_display

PROJ = Path(__file__).resolve().parents[1]


def ctx_for(store, cfg, payloads, now):
    return Context(
        cfg,
        EnvSettings(),
        store,
        client_factory=lambda: HttpClient(fast_http(), transport=mock_transport(payloads)),
        now=lambda: now,
    )


GH = "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs"


def gh_payload(*ids):
    return {
        GH: {
            "jobs": [
                {"id": i, "title": f"Job {i}", "location": {"name": "Austin, TX"}} for i in ids
            ]
        }
    }


def test_collect_isolates_failing_boards_and_records_stats(store, cfg):
    add_board(store)
    add_board(store, ATSKind.LEVER, "missingco", "Missing Co", "missing.example")
    res = collect(ctx_for(store, cfg, gh_payload(1, 2), T0))
    assert res["status"] == "success"  # NOT_FOUND is a real answer, not an outage
    assert res["sources_attempted"] == 2 and res["sources_succeeded"] == 1
    assert res["jobs_fetched"] == 2 and res["requests"] >= 2
    assert res["outcomes"] == {"success": 1, "not_found": 1}
    run = store.one("SELECT * FROM runs WHERE kind='collect'")
    assert run["status"] == "success" and run["finished_at"]


def test_rerun_after_crash_is_safe(store, cfg):
    add_board(store)
    collect(ctx_for(store, cfg, gh_payload(1), T0))
    store.conn.execute(
        "INSERT INTO runs(id, kind, started_at, status) VALUES ('dead','collect','x','running')"
    )
    collect(ctx_for(store, cfg, gh_payload(1, 2), T0 + timedelta(days=1)))
    collect(ctx_for(store, cfg, gh_payload(1, 2), T0 + timedelta(days=1, minutes=5)))  # repeat
    assert store.one("SELECT count(*) n FROM jobs")["n"] == 2
    assert store.one("SELECT count(*) n FROM jobs WHERE is_new=1")["n"] == 1
    assert store.one("SELECT status FROM runs WHERE id='dead'")["status"] == "interrupted"


def test_publish_window_has_no_gaps_and_repeat_is_idempotent(store, cfg, tmp_path):
    cfg.publish.csv_dir = str(tmp_path / "exp")
    add_board(store)
    collect(ctx_for(store, cfg, gh_payload(1), T0))
    collect(ctx_for(store, cfg, gh_payload(1, 2), T0 + timedelta(days=1)))
    p1 = publish(
        Context(cfg, EnvSettings(), store, now=lambda: T0 + timedelta(days=1, hours=1)),
        sheets=False,
    )
    assert p1["csv_NewJobs"] == 1
    collect(ctx_for(store, cfg, gh_payload(1, 2, 3), T0 + timedelta(days=2)))
    # not due yet (every 3 days) -> nothing published, window not advanced
    p2 = publish(
        Context(cfg, EnvSettings(), store, now=lambda: T0 + timedelta(days=2, hours=1)),
        sheets=False,
    )
    assert "csv:not-due" in p2["publication"]
    collect(ctx_for(store, cfg, gh_payload(1, 2, 3, 4), T0 + timedelta(days=3)))
    p3 = publish(
        Context(cfg, EnvSettings(), store, now=lambda: T0 + timedelta(days=4, hours=1)),
        sheets=False,
    )
    assert p3["csv_NewJobs"] == 2  # jobs 3 and 4: nothing lost across the skipped day


def test_sheets_credential_failure_is_distinct_and_does_not_advance(store, cfg, tmp_path):
    cfg.publish.csv_dir = str(tmp_path / "exp")
    res = publish(Context(cfg, EnvSettings(_env_file=None), store, now=lambda: T0), sheets=True)
    assert "sheets:credential-error" in res["publication"] and res["status"] == "failed"
    row = store.one("SELECT * FROM publish_state WHERE target='sheets'")
    assert row["last_success_at"] is None and row["last_error"].startswith("credential")
    assert cli._exit_code(res) == 4


def test_due_logic_tolerates_dst_and_late_starts():
    # Fall-back week: 3 "days" of wall-clock schedule = 73h; a late start must still count as due.
    from jobmonitor.db import Store

    s = Store(":memory:")
    last = datetime(2026, 10, 30, 16, 7, tzinfo=UTC)
    s.conn.execute(
        "INSERT INTO publish_state(target, last_success_at) VALUES ('csv', ?)", (last.isoformat(),)
    )
    assert publish_due(s, "csv", 3, last + timedelta(days=3, hours=1))
    assert publish_due(s, "csv", 3, last + timedelta(days=3) - timedelta(hours=2))  # cron jitter
    assert not publish_due(s, "csv", 3, last + timedelta(days=2))


@pytest.mark.parametrize(
    "utc,expected",
    [
        (datetime(2026, 7, 1, 16, 7, tzinfo=UTC), "2026-07-01 09:07 PDT"),
        (datetime(2026, 12, 1, 17, 7, tzinfo=UTC), "2026-12-01 09:07 PST"),
    ],
)
def test_pacific_display_dst(utc, expected):
    assert pacific_display(utc) == expected


def test_workflow_schedules_use_la_timezone():
    wf = yaml.safe_load((PROJ / ".github/workflows/scheduled.yml").read_text(encoding="utf-8"))
    on = wf.get("on") or wf.get(True)  # PyYAML parses bare `on` as True
    for entry in on["schedule"]:
        assert entry["timezone"] == "America/Los_Angeles"
        minute, hour = entry["cron"].split()[:2]
        assert minute != "0"  # avoid top-of-hour load
        # 09:xx local is never inside a DST gap (02:00-03:00), so runs are never skipped/doubled
        assert hour not in ("1", "2")


def test_workflows_are_hardened():
    for path in (PROJ / ".github/workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        wf = yaml.safe_load(text)
        assert "permissions" in wf, path.name
        for job in wf["jobs"].values():
            if "uses" in job:  # caller of the reusable workflow; timeout lives there
                assert job["uses"] == "./.github/workflows/_job.yml"
                continue
            assert "timeout-minutes" in job, path.name
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses:
                    ref = uses.split("@")[1]
                    assert len(ref.split()[0]) == 40, f"{path.name}: {uses} not pinned to SHA"
        for line in text.splitlines():  # secrets / inputs never interpolated into a shell line
            if line.strip().startswith(("run:", "jobmonitor ", "case ", "python -m")):
                assert "${{" not in line, f"{path.name}: {line.strip()}"


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("discover", "collect", "publish", "run", "state", "demo"):
        assert cmd in out


def test_demo_runs_offline(tmp_path, monkeypatch):
    import socket

    def no_network(*a, **k):
        raise AssertionError("demo must not touch the network")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    assert cli.main(["--log-level", "WARNING", "demo", "--out", str(tmp_path / "d")]) == 0
    assert (tmp_path / "d/exports/new_jobs.csv").exists()


def test_pacific_zone_available():
    assert PACIFIC.key == "America/Los_Angeles"
