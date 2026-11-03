"""Outbound URL / destination validation (SSRF defense).

Two layers:
1. `validate_url` - syntactic checks on every URL and every redirect target.
2. `GuardedBackend` - validates the *actual IP* at TCP connect time, after our own DNS
   resolution, and connects to that exact IP. TLS SNI / Host still use the hostname, so
   certificate verification is unaffected. Because the checked address is the connected
   address, DNS rebinding between "check" and "use" is not possible.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpcore

BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan", ".corp")
BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "metadata"}


class BlockedURL(Exception):
    """Raised when a URL or resolved destination violates policy."""


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped or ip.sixtofour
        if mapped is not None:
            return _ip_is_public(mapped)
        if ip.teredo:
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def check_ip(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError as e:
        raise BlockedURL(f"invalid IP {addr!r}") from e
    if not _ip_is_public(ip):
        raise BlockedURL(f"non-public destination {ip}")
    return ip


@dataclass(frozen=True)
class SourcePolicy:
    """Explicit per-source network policy."""

    name: str
    # None = any public host; otherwise host must equal or be a subdomain of one of these.
    allowed_hosts: tuple[str, ...] | None = None
    allow_http: bool = False  # plain http only if explicitly allowed
    check_robots: bool = False
    max_bytes: int | None = None
    methods: frozenset[str] = field(default_factory=lambda: frozenset({"GET"}))

    def host_allowed(self, host: str) -> bool:
        if self.allowed_hosts is None:
            return True
        return any(host == h or host.endswith("." + h) for h in self.allowed_hosts)


def validate_url(url: str, policy: SourcePolicy | None = None) -> str:
    """Return the normalized hostname or raise BlockedURL."""
    if not isinstance(url, str) or len(url) > 4096:
        raise BlockedURL("url missing or too long")
    if any(ch in url for ch in ("\r", "\n", "\t", " ", "\\")):
        raise BlockedURL("url contains whitespace or backslash")
    try:
        parts = urlsplit(url)
        port = parts.port  # raises on bad port
    except ValueError as e:
        raise BlockedURL(f"unparseable url: {e}") from e
    scheme = parts.scheme.lower()
    allowed_schemes = {"https", "http"} if (policy is None or policy.allow_http) else {"https"}
    if scheme not in allowed_schemes:
        raise BlockedURL(f"scheme {scheme!r} not allowed")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BlockedURL("credentials in url not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURL("missing host")
    if port is not None and port not in (80, 443):
        raise BlockedURL(f"port {port} not allowed")
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise BlockedURL(f"host {host!r} not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # hostname: purely-numeric/hex labels are a common obfuscation trick
        if host.replace(".", "").isdigit() or host.startswith("0x"):
            raise BlockedURL(f"numeric host {host!r} not allowed") from None
        if "." not in host:
            raise BlockedURL(f"single-label host {host!r} not allowed") from None
    else:
        check_ip(host)
    if policy is not None and not policy.host_allowed(host):
        raise BlockedURL(f"host {host!r} outside policy {policy.name!r}")
    return host


def resolve_public(host: str, port: int) -> list[str]:
    """Resolve and return only addresses that pass `check_ip`; raise if any address is non-public.

    Rejecting the whole name when *any* record is private avoids mixed-record tricks.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise httpcore.ConnectError(f"dns resolution failed for {host}: {e}") from e
    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        check_ip(addr)
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise BlockedURL(f"no addresses for {host}")
    return addrs


class GuardedBackend(httpcore.SyncBackend):
    resolver = staticmethod(resolve_public)

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last_exc: Exception | None = None
        for addr in self.resolver(host, port):
            try:
                return super().connect_tcp(addr, port, timeout, local_address, socket_options)
            except httpcore.ConnectError as e:  # try next validated address
                last_exc = e
        raise last_exc or httpcore.ConnectError(f"could not connect to {host}")

    def connect_unix_socket(self, *args, **kwargs):  # pragma: no cover - defensive
        raise BlockedURL("unix sockets not allowed")
