"""Opt-in network smoke tests: `pytest -m network`. One small request per source."""

import pytest

from jobmonitor.adapters import ADAPTERS
from jobmonitor.config import DiscoveryConfig, HttpConfig
from jobmonitor.discovery.yc import YCSource
from jobmonitor.enums import ATSKind
from jobmonitor.http_client import HttpClient

pytestmark = pytest.mark.network

# Public boards verified during development; replace if one disappears.
BOARDS = [(ATSKind.GREENHOUSE, "anthropic"), (ATSKind.LEVER, "leverdemo"), (ATSKind.ASHBY, "ramp")]


@pytest.fixture(scope="module")
def client():
    with HttpClient(HttpConfig(per_host_min_interval_seconds=1.0, max_retries=1)) as c:
        yield c


@pytest.mark.parametrize("kind,board", BOARDS)
def test_live_board(client, kind, board):
    snap = ADAPTERS[kind].scan(client, board)
    assert snap.complete, snap.error
    assert all(j.source_job_id and j.title for j in snap.jobs)


def test_live_yc_batch(client):
    res = YCSource().discover(
        client, DiscoveryConfig(yc_batches=["summer-2025"], max_companies_per_source=5)
    )
    assert res.ok and res.companies
