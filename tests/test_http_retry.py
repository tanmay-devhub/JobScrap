import random
import threading
import time

import httpx
import pytest

from conftest import fast_http, mock_client
from jobmonitor.http_client import FetchError, HostLimiter, backoff_delay, parse_retry_after
from jobmonitor.netguard import SourcePolicy

P = SourcePolicy("t")


def seq(*responses):
    it = iter(responses)
    calls = []

    def handler(req):
        calls.append(req)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    return handler, calls


def test_retry_then_success_with_retry_after():
    handler, calls = seq(
        httpx.Response(429, headers={"retry-after": "7"}),
        httpx.Response(503),
        httpx.Response(200, json={"ok": 1}),
    )
    sleeps = []
    c = mock_client(handler, fast_http(max_retry_after_seconds=60), sleeps)
    assert c.fetch("https://a.example/", P).json() == {"ok": 1}
    assert len(calls) == 3 and sleeps[0] == 7.0 and 0 <= sleeps[1] <= 0.02
    assert c.stats.retries == 2 and c.stats.requests == 3


def test_retry_after_is_capped():
    handler, _ = seq(httpx.Response(503, headers={"retry-after": "100000"}), httpx.Response(200))
    sleeps = []
    mock_client(
        handler, fast_http(max_retry_after_seconds=5, request_deadline_seconds=60), sleeps
    ).fetch("https://a.example/", P)
    assert sleeps == [5.0]


def test_non_retryable_status_fails_fast():
    handler, calls = seq(httpx.Response(403), httpx.Response(200))
    with pytest.raises(FetchError) as e:
        mock_client(handler).fetch("https://a.example/", P)
    assert e.value.status == 403 and len(calls) == 1


def test_retries_exhausted():
    handler, calls = seq(*[httpx.Response(502)] * 10)
    with pytest.raises(FetchError):
        mock_client(handler, fast_http(max_retries=2)).fetch("https://a.example/", P)
    assert len(calls) == 3


def test_transport_errors_are_retried():
    handler, calls = seq(
        httpx.ConnectError("reset"), httpx.ReadTimeout("slow"), httpx.Response(200)
    )
    assert mock_client(handler).fetch("https://a.example/", P).status == 200
    assert len(calls) == 3


def test_parse_retry_after_http_date():
    from datetime import timedelta
    from email.utils import format_datetime

    from jobmonitor.timeutil import utcnow

    value = format_datetime(utcnow() + timedelta(seconds=30), usegmt=True)
    assert 25 <= parse_retry_after(value, 120) <= 30
    assert parse_retry_after("garbage", 120) is None
    assert parse_retry_after("-5", 120) == 0


def test_backoff_is_bounded_full_jitter():
    rng = random.Random(1)
    for attempt in range(10):
        d = backoff_delay(attempt, 1.0, 30.0, rng)
        assert 0 <= d <= min(30.0, 2**attempt)


def test_host_limiter_enforces_interval_and_concurrency():
    lim = HostLimiter(per_host_concurrency=1, min_interval=0.05)
    starts = []
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def work():
        sem = lim.acquire("h")
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            starts.append(time.monotonic())
        time.sleep(0.01)
        with lock:
            active["n"] -= 1
        sem.release()

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    starts.sort()
    assert active["max"] == 1
    assert all(b - a >= 0.045 for a, b in zip(starts, starts[1:], strict=False))
