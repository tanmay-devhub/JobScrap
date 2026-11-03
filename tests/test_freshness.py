from datetime import timedelta

import pytest

from conftest import T0, add_board
from jobmonitor.adapters.base import BoardSnapshot
from jobmonitor.db import LockHeld
from jobmonitor.enums import ScanOutcome
from jobmonitor.freshness import apply_snapshot
from jobmonitor.models import RawJob


def rj(i, title=None, published=None, updated=None, location="Austin, TX"):
    return RawJob(
        source_job_id=str(i),
        title=title or f"Job {i}",
        location=location,
        published_at=published,
        updated_at=updated,
    )


def ok(*jobs):
    return BoardSnapshot(ScanOutcome.SUCCESS if jobs else ScanOutcome.ZERO_JOBS, jobs=list(jobs))


def jobs_by_sid(store):
    return {r["source_job_id"]: dict(r) for r in store.q("SELECT * FROM jobs")}


def board(store):
    return dict(store.one("SELECT * FROM boards"))


def test_first_scan_is_baseline_but_recent_dated_jobs_are_new(store, cfg):
    b, _ = add_board(store)
    snap = ok(
        rj(1), rj(2, published=T0 - timedelta(days=90)), rj(3, published=T0 - timedelta(hours=30))
    )
    res = apply_snapshot(store, b, snap, T0, cfg)
    j = jobs_by_sid(store)
    assert (j["1"]["freshness_basis"], j["1"]["is_new"]) == ("baseline", 0)
    assert (j["2"]["freshness_basis"], j["2"]["is_new"]) == ("baseline", 0)
    assert (j["3"]["freshness_basis"], j["3"]["is_new"]) == ("source_published", 1)
    assert res.new == 1 and res.baseline == 2
    assert board(store)["baseline_done"] == 1


def test_later_scan_labels_new_jobs(store, cfg):
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1)), T0, cfg)
    t1 = T0 + timedelta(days=1)
    apply_snapshot(
        store,
        b,
        ok(
            rj(1),
            rj(2),
            rj(3, published=t1 - timedelta(hours=2)),
            rj(4, published=T0 - timedelta(days=200)),
        ),
        t1,
        cfg,
    )
    j = jobs_by_sid(store)
    assert (j["2"]["freshness_basis"], j["2"]["is_new"]) == ("newly_detected", 1)
    assert (j["3"]["freshness_basis"], j["3"]["is_new"]) == ("source_published", 1)
    # an old publication date that we only now see: new to us, but not labelled as freshly published
    assert (j["4"]["freshness_basis"], j["4"]["is_new"]) == ("newly_detected", 1)
    assert j["1"]["is_new"] == 0


def test_updated_at_and_republish_never_reset_creation(store, cfg):
    b, _ = add_board(store)
    pub = T0 - timedelta(days=30)
    apply_snapshot(store, b, ok(rj(1, published=pub)), T0, cfg)
    t1 = T0 + timedelta(days=1)
    apply_snapshot(store, b, ok(rj(1, published=t1, updated=t1)), t1, cfg)  # republished
    j = jobs_by_sid(store)["1"]
    assert j["source_published_at"] == pub.isoformat(timespec="seconds")
    assert j["source_updated_at"] == t1.isoformat(timespec="seconds")
    assert j["first_seen_at"] == T0.isoformat(timespec="seconds")
    assert j["is_new"] == 0 and j["freshness_basis"] == "baseline"


@pytest.mark.parametrize(
    "outcome",
    [ScanOutcome.SOURCE_ERROR, ScanOutcome.PARTIAL, ScanOutcome.BLOCKED, ScanOutcome.NOT_FOUND],
)
def test_failed_or_partial_scan_changes_nothing(store, cfg, outcome):
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1), rj(2)), T0, cfg)
    before_jobs, before_board = jobs_by_sid(store), board(store)
    t1 = T0 + timedelta(days=1)
    # partial snapshot even carries some jobs - they must not be applied
    res = apply_snapshot(store, b, BoardSnapshot(outcome, jobs=[rj(9)], error="boom"), t1, cfg)
    after = board(store)
    assert not res.checkpoint_advanced
    assert jobs_by_sid(store) == before_jobs
    assert after["last_success_at"] == before_board["last_success_at"]
    assert after["consecutive_failures"] == 1 and after["last_outcome"] == outcome.value


def test_removal_requires_threshold_of_complete_snapshots(store, cfg):
    cfg.freshness.close_after_missing_scans = 2
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1), rj(2)), T0, cfg)
    apply_snapshot(store, b, ok(rj(1)), T0 + timedelta(days=1), cfg)
    assert jobs_by_sid(store)["2"]["status"] == "open"
    # failure in between does not count
    apply_snapshot(store, b, BoardSnapshot(ScanOutcome.SOURCE_ERROR), T0 + timedelta(days=2), cfg)
    assert jobs_by_sid(store)["2"]["missing_scans"] == 1
    apply_snapshot(store, b, ok(rj(1)), T0 + timedelta(days=3), cfg)
    assert jobs_by_sid(store)["2"]["status"] == "closed"


def test_zero_jobs_snapshot_counts_toward_close(store, cfg):
    cfg.freshness.close_after_missing_scans = 2
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1)), T0, cfg)
    apply_snapshot(store, b, ok(), T0 + timedelta(days=1), cfg)
    apply_snapshot(store, b, ok(), T0 + timedelta(days=2), cfg)
    assert jobs_by_sid(store)["1"]["status"] == "closed"


def test_reappearing_job_reopens_without_being_new(store, cfg):
    cfg.freshness.close_after_missing_scans = 1
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1)), T0, cfg)
    apply_snapshot(store, b, ok(), T0 + timedelta(days=1), cfg)
    res = apply_snapshot(store, b, ok(rj(1)), T0 + timedelta(days=2), cfg)
    j = jobs_by_sid(store)["1"]
    assert (
        j["status"] == "open" and j["closed_at"] is None and j["is_new"] == 0 and res.reopened == 1
    )


def test_repost_under_new_id_is_labelled_not_new(store, cfg):
    cfg.freshness.close_after_missing_scans = 1
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1, title="Data Engineer")), T0, cfg)
    apply_snapshot(store, b, ok(), T0 + timedelta(days=1), cfg)
    res = apply_snapshot(store, b, ok(rj(2, title="Data Engineer")), T0 + timedelta(days=2), cfg)
    j = jobs_by_sid(store)
    assert j["2"]["repost_of"] == j["1"]["job_id"] and j["2"]["is_new"] == 0 and res.reposts == 1


def test_distinct_requisitions_with_same_title_are_kept(store, cfg):
    b, _ = add_board(store)
    apply_snapshot(
        store, b, ok(rj(1, title="SWE"), rj(2, title="SWE"), rj(2, title="SWE")), T0, cfg
    )
    assert len(jobs_by_sid(store)) == 2  # same title kept; duplicate id within payload collapsed


def test_not_modified_keeps_jobs_and_advances_checkpoint(store, cfg):
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1)), T0, cfg)
    t1 = T0 + timedelta(days=1)
    res = apply_snapshot(store, b, BoardSnapshot(ScanOutcome.SUCCESS, not_modified=True), t1, cfg)
    j = jobs_by_sid(store)["1"]
    assert res.checkpoint_advanced and j["status"] == "open"
    assert j["last_seen_at"] == t1.isoformat(timespec="seconds")
    assert board(store)["last_success_at"] == t1.isoformat(timespec="seconds")


def test_checkpoint_overlap_window(store, cfg):
    cfg.freshness.checkpoint_overlap_hours = 6
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1)), T0, cfg)
    t1 = T0 + timedelta(days=3)  # weekend gap: covered because window starts at the checkpoint
    apply_snapshot(
        store,
        b,
        ok(
            rj(1),
            rj(2, published=T0 - timedelta(hours=5)),
            rj(3, published=T0 - timedelta(hours=7)),
        ),
        t1,
        cfg,
    )
    j = jobs_by_sid(store)
    assert j["2"]["freshness_basis"] == "source_published"  # within overlap of checkpoint
    assert j["3"]["freshness_basis"] == "newly_detected"


def test_not_found_repeatedly_disables_board(store, cfg):
    cfg.freshness.disable_board_after_not_found = 2
    b, _ = add_board(store)
    for d in range(2):
        apply_snapshot(store, b, BoardSnapshot(ScanOutcome.NOT_FOUND), T0 + timedelta(days=d), cfg)
    assert board(store)["enabled"] == 0


def test_interrupted_run_is_marked_and_lock_blocks_overlap(store):
    store.conn.execute(
        "INSERT INTO runs(id, kind, started_at, status) VALUES ('r1','collect','x','running')"
    )
    with store.lock("collect"):
        assert store.one("SELECT status FROM runs WHERE id='r1'")["status"] == "interrupted"
        with pytest.raises(LockHeld), store.lock("collect"):
            pass
    with store.lock("collect"):  # released after use
        pass


def test_reclassify_applies_new_rules_offline(store, cfg):
    from jobmonitor.freshness import reclassify

    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1, location="Remote - Worldwide")), T0, cfg)
    assert jobs_by_sid(store)["1"]["eligibility"] == "location_unclear"
    cfg.filters.worldwide_remote_counts_as_us = True
    assert reclassify(store, cfg) == {"jobs": 1, "changed": 1}
    assert jobs_by_sid(store)["1"]["eligibility"] == "remote_us_ok"


def test_crash_mid_apply_rolls_back(store, cfg, monkeypatch):
    b, _ = add_board(store)
    apply_snapshot(store, b, ok(rj(1)), T0, cfg)
    import jobmonitor.freshness as fr

    calls = {"n": 0}
    real = fr._upsert_job

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash")
        return real(*a, **k)

    monkeypatch.setattr(fr, "_upsert_job", flaky)
    with pytest.raises(RuntimeError):
        apply_snapshot(store, b, ok(rj(1), rj(2), rj(3)), T0 + timedelta(days=1), cfg)
    assert set(jobs_by_sid(store)) == {"1"}  # nothing half-applied
    assert board(store)["last_success_at"] == T0.isoformat(timespec="seconds")
