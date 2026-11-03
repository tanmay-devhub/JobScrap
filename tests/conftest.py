import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from jobmonitor.config import AppConfig, HttpConfig
from jobmonitor.db import Store
from jobmonitor.enums import ATSKind
from jobmonitor.http_client import HttpClient
from jobmonitor.models import DiscoveredCompany

FIX = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 2, 17, 0, tzinfo=UTC)  # Monday 09:00 PST


def load(name: str):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


def fast_http(**kw) -> HttpConfig:
    base = dict(
        per_host_min_interval_seconds=0,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        max_retries=3,
    )
    base.update(kw)
    return HttpConfig(**base)


def mock_client(handler, cfg: HttpConfig | None = None, sleeps: list | None = None) -> HttpClient:
    sleeps = sleeps if sleeps is not None else []
    return HttpClient(
        cfg or fast_http(), transport=httpx.MockTransport(handler), sleep=sleeps.append
    )


@pytest.fixture
def cfg() -> AppConfig:
    c = AppConfig()
    c.http = fast_http()
    return c


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "t.sqlite3")
    yield s
    s.close()


def add_board(
    store: Store,
    ats=ATSKind.GREENHOUSE,
    board="examplerobotics",
    name="Example Robotics",
    domain="examplerobotics.example",
):
    cid = store.upsert_company(
        DiscoveredCompany(name=name, domain=domain, ats=ats, board=board, source="test"), T0
    )
    return dict(store.one("SELECT * FROM boards WHERE ats=? AND board=?", (ats.value, board))), cid
