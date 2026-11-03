import csv
from datetime import timedelta

from conftest import T0, add_board
from jobmonitor.adapters.base import BoardSnapshot
from jobmonitor.enums import ScanOutcome
from jobmonitor.exports.csv_export import write_csv
from jobmonitor.exports.views import jobs_view, new_jobs_view, unclear_view
from jobmonitor.freshness import apply_snapshot
from jobmonitor.models import RawJob


def seed(store, cfg):
    b, _ = add_board(store)
    jobs = [
        RawJob(
            source_job_id="1",
            title="=cmd|' /C calc'!A0",
            location="Austin, TX",
            job_url="javascript:alert(1)",
            apply_url="https://x.example/apply",
        ),
        RawJob(source_job_id="2", title="Senior Engineer", location="Remote", department="Eng"),
        RawJob(source_job_id="3", title="Intern", location="London, UK"),
        RawJob(
            source_job_id="4",
            title="Clearance Engineer",
            location="Denver, CO",
            description_text="Must be a U.S. citizen",
        ),
        RawJob(source_job_id="5", title="Software Intern", location="Boston, MA"),
    ]
    apply_snapshot(
        store, b, BoardSnapshot(ScanOutcome.SUCCESS, jobs=[]), T0, cfg
    )  # baseline (empty)
    apply_snapshot(
        store, b, BoardSnapshot(ScanOutcome.SUCCESS, jobs=jobs), T0 + timedelta(days=1), cfg
    )


def test_views_route_eligibility(store, cfg):
    seed(store, cfg)
    now = T0 + timedelta(days=1, hours=1)
    jobs = {r[12]: r for r in jobs_view(store, cfg.filters, now).rows}
    assert set(jobs) == {"1", "4", "5"}  # US only; London excluded; Remote -> unclear
    assert {r[12] for r in unclear_view(store, cfg.filters).rows} == {"2"}
    new = new_jobs_view(store, cfg.filters, T0, now)
    assert {r[12] for r in new.rows} == {"1", "4", "5"} and new.header[-1] == "publish_window"


def test_untrusted_cells_are_neutralized_and_links_validated(store, cfg):
    seed(store, cfg)
    row = next(
        r for r in jobs_view(store, cfg.filters, T0 + timedelta(days=2)).rows if r[12] == "1"
    )
    assert row[2].startswith("'=")
    assert row[13] == ""  # javascript: link dropped
    assert row[14] == "https://x.example/apply"


def test_optional_filters_never_drop_unknowns(store, cfg):
    seed(store, cfg)
    now = T0 + timedelta(days=2)
    cfg.filters.seniority_exclude = ["intern"]
    cfg.filters.exclude_citizenship_required = True
    cfg.filters.exclude_no_sponsorship = True
    ids = {r[12] for r in jobs_view(store, cfg.filters, now).rows}
    assert ids == {"1"}  # intern + citizenship-required removed; unknown values kept


def test_csv_written_atomically(store, cfg, tmp_path):
    seed(store, cfg)
    view = jobs_view(store, cfg.filters, T0 + timedelta(days=2))
    path = write_csv(view, tmp_path / "out")
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == view.header and len(rows) == len(view.rows) + 1
    assert not list((tmp_path / "out").glob(".tmp-*"))
