"""Guarded, rate-limited, retrying HTTP client (sync; thread-safe)."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import HttpConfig
from .netguard import BlockedURL, GuardedBackend, SourcePolicy, validate_url
from .timeutil import utcnow

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


class FetchError(Exception):
    """Source-side failure (network, HTTP status, size, deadline)."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class ResponseTooLarge(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    headers: httpx.Headers
    content: bytes

    def json(self):
        import json

        return json.loads(self.content)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass
class RequestStats:
    requests: int = 0
    retries: int = 0
    errors: int = 0
    not_modified: int = 0
    blocked: int = 0
    by_host: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def bump(self, attr: str, host: str | None = None) -> None:
        with self._lock:
            setattr(self, attr, getattr(self, attr) + 1)
            if host and attr == "requests":
                self.by_host[host] += 1

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "errors": self.errors,
            "not_modified": self.not_modified,
            "blocked": self.blocked,
            "by_host": dict(self.by_host),
        }


class HostLimiter:
    """Per-host concurrency cap + minimum interval between request starts."""

    def __init__(self, per_host_concurrency: int, min_interval: float):
        self._sem: dict[str, threading.BoundedSemaphore] = {}
        self._next: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()
        self._conc = per_host_concurrency
        self._interval = min_interval

    def acquire(self, host: str) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._sem.setdefault(host, threading.BoundedSemaphore(self._conc))
        sem.acquire()
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next[host])
            self._next[host] = start + self._interval
        if start > now:
            time.sleep(start - now)
        return sem


def parse_retry_after(value: str | None, cap: float) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = (when - utcnow()).total_seconds()
    return max(0.0, min(seconds, cap))


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Full-jitter exponential backoff: uniform(0, min(cap, base * 2**attempt))."""
    rng = rng or random
    return rng.uniform(0, min(cap, base * (2**attempt)))


def build_transport() -> httpx.HTTPTransport:
    """HTTPTransport whose connection pool uses GuardedBackend. Fails closed if unsupported."""
    transport = httpx.HTTPTransport(verify=True, retries=0, http2=False)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError("cannot install guarded network backend; refusing to run unprotected")
    pool._network_backend = GuardedBackend()
    return transport


class HttpClient:
    def __init__(
        self,
        cfg: HttpConfig,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self.cfg = cfg
        self.stats = RequestStats()
        self._sleep = sleep
        self._limiter = HostLimiter(cfg.per_host_concurrency, cfg.per_host_min_interval_seconds)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self._client = httpx.Client(
            transport=transport or build_transport(),
            timeout=httpx.Timeout(cfg.timeout_seconds),
            follow_redirects=False,  # every hop is validated manually
            trust_env=False,  # env proxies would bypass destination checks
            headers={"User-Agent": cfg.user_agent, "Accept-Encoding": "gzip, deflate"},
            limits=httpx.Limits(
                max_connections=cfg.max_concurrency * 2, max_keepalive_connections=10
            ),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- robots ----
    def robots_allowed(self, url: str, policy: SourcePolicy) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            cached = self._robots.get(origin, ...)
        if cached is ...:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                res = self._fetch_once(urljoin(origin, "/robots.txt"), policy, "GET", None, {})
                if res.status == 200:
                    rp.parse(res.text.splitlines())
                elif res.status in (401, 403):
                    rp.disallow_all = True
                else:  # 404 etc: no restrictions
                    rp.allow_all = True
            except (FetchError, BlockedURL):
                rp = None  # unknown -> treat as disallowed (conservative)
            with self._robots_lock:
                self._robots[origin] = rp
            cached = rp
        if cached is None:
            return False
        return cached.can_fetch(self.cfg.user_agent, url)

    # ---- core ----
    def fetch(
        self,
        url: str,
        policy: SourcePolicy,
        method: str = "GET",
        json_body: dict | None = None,
        headers: dict[str, str] | None = None,
        allow_statuses: frozenset[int] = frozenset({200}),
    ) -> FetchResult:
        """Fetch with retries. Statuses in `allow_statuses` (plus 304) are returned, others raise."""
        if method not in policy.methods:
            raise BlockedURL(f"method {method} not allowed by policy {policy.name}")
        try:
            validate_url(url, policy)
        except BlockedURL:
            self.stats.bump("blocked")
            raise
        if policy.check_robots and not self.robots_allowed(url, policy):
            self.stats.bump("blocked")
            raise BlockedURL(f"robots.txt disallows {url}")

        deadline = time.monotonic() + self.cfg.request_deadline_seconds * (self.cfg.max_retries + 1)
        attempt = 0
        while True:
            try:
                res = self._fetch_once(url, policy, method, json_body, headers or {})
            except FetchError as e:
                if not e.retryable or attempt >= self.cfg.max_retries:
                    self.stats.bump("errors")
                    raise
                delay = backoff_delay(
                    attempt, self.cfg.backoff_base_seconds, self.cfg.backoff_max_seconds
                )
            else:
                if res.status in allow_statuses or res.status == 304:
                    if res.status == 304:
                        self.stats.bump("not_modified")
                    return res
                retryable = res.status in RETRYABLE_STATUS
                if not retryable or attempt >= self.cfg.max_retries:
                    self.stats.bump("errors")
                    raise FetchError(f"HTTP {res.status} for {url}", res.status, retryable)
                delay = parse_retry_after(
                    res.headers.get("retry-after"), self.cfg.max_retry_after_seconds
                )
                if delay is None:
                    delay = backoff_delay(
                        attempt, self.cfg.backoff_base_seconds, self.cfg.backoff_max_seconds
                    )
            if time.monotonic() + delay > deadline:
                self.stats.bump("errors")
                raise FetchError(f"retry budget exhausted for {url}", retryable=False)
            attempt += 1
            self.stats.bump("retries")
            log.debug("retrying %s in %.1fs (attempt %d)", url, delay, attempt)
            self._sleep(delay)

    def _fetch_once(self, url, policy, method, json_body, headers) -> FetchResult:
        max_bytes = policy.max_bytes or self.cfg.max_response_bytes
        current = url
        for _hop in range(self.cfg.max_redirects + 1):
            host = validate_url(current, policy)
            sem = self._limiter.acquire(host)
            try:
                self.stats.bump("requests", host)
                start = time.monotonic()
                req = self._client.build_request(method, current, json=json_body, headers=headers)
                try:
                    resp = self._client.send(req, stream=True)
                except BlockedURL:
                    self.stats.bump("blocked")
                    raise
                except httpx.TimeoutException as e:
                    raise FetchError(f"timeout: {type(e).__name__}", retryable=True) from e
                except httpx.TransportError as e:
                    raise FetchError(f"transport error: {type(e).__name__}", retryable=True) from e
                try:
                    if resp.status_code in REDIRECT_STATUS:
                        loc = resp.headers.get("location")
                        if not loc:
                            raise FetchError(f"redirect without location from {current}")
                        current = urljoin(current, loc)
                        if resp.status_code == 303:
                            method, json_body = "GET", None
                        continue
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise ResponseTooLarge(f"response too large ({declared} bytes)")
                    buf = bytearray()
                    for chunk in resp.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            raise ResponseTooLarge(f"response exceeded {max_bytes} bytes")
                        if time.monotonic() - start > self.cfg.request_deadline_seconds:
                            raise FetchError("request deadline exceeded", retryable=True)
                    return FetchResult(current, resp.status_code, resp.headers, bytes(buf))
                except httpx.TransportError as e:
                    raise FetchError(f"read error: {type(e).__name__}", retryable=True) from e
                finally:
                    resp.close()
            finally:
                sem.release()
        raise FetchError(f"too many redirects from {url}")
