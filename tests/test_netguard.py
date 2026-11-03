import httpcore
import httpx
import pytest

from conftest import fast_http, mock_client
from jobmonitor.http_client import FetchError, HttpClient, ResponseTooLarge, build_transport
from jobmonitor.netguard import BlockedURL, GuardedBackend, SourcePolicy, check_ip, validate_url

ANY = SourcePolicy("any")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com",
        "javascript:alert(1)",
        "http://example.com/",  # plain http not allowed by default policy
        "https://user:pw@example.com/",
        "https://token@example.com/",
        "https://localhost/",
        "https://foo.localhost/",
        "https://printer.local/",
        "https://db.internal/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://127.0.0.1/",
        "https://10.0.0.5/",
        "https://192.168.1.1/",
        "https://172.16.0.1/",
        "https://169.254.169.254/latest/meta-data/",
        "https://100.64.0.1/",
        "https://0.0.0.0/",
        "https://[::1]/",
        "https://[fd00::1]/",
        "https://[fe80::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://2130706433/",
        "https://0x7f000001/",
        "https://intranet/",
        "https://example.com:8443/",
        "https://exa mple.com/",
        "https://example.com\\@evil.com/",
    ],
)
def test_blocked_urls(url):
    with pytest.raises(BlockedURL):
        validate_url(url, ANY)


def test_allowed_url_and_policy_hosts():
    assert (
        validate_url("https://boards-api.greenhouse.io/v1/boards/x/jobs", ANY)
        == "boards-api.greenhouse.io"
    )
    gh = SourcePolicy("gh", allowed_hosts=("boards-api.greenhouse.io",))
    with pytest.raises(BlockedURL):
        validate_url("https://evil.example/", gh)
    assert validate_url("http://example.com/", SourcePolicy("h", allow_http=True)) == "example.com"


@pytest.mark.parametrize("ip", ["8.8.8.8", "2606:4700:4700::1111"])
def test_public_ips_pass(ip):
    check_ip(ip)


def test_redirect_to_private_target_is_blocked():
    def handler(req):
        if req.url.host == "start.example":
            return httpx.Response(
                302, headers={"location": "https://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(200, text="SECRET")

    with pytest.raises(BlockedURL):
        mock_client(handler).fetch("https://start.example/", ANY)


def test_redirect_outside_policy_and_too_many_redirects():
    gh = SourcePolicy("gh", allowed_hosts=("a.example",))

    def handler(req):
        return httpx.Response(301, headers={"location": "https://b.example/"})

    with pytest.raises(BlockedURL):
        mock_client(handler).fetch("https://a.example/", gh)

    def loop(req):
        return httpx.Response(302, headers={"location": "/again"})

    with pytest.raises(FetchError, match="too many redirects"):
        mock_client(loop).fetch("https://a.example/", ANY)


def test_redirect_followed_when_safe():
    def handler(req):
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text="ok")

    res = mock_client(handler).fetch("https://a.example/old", ANY)
    assert res.url == "https://a.example/new" and res.text == "ok"


def test_response_size_limit():
    big = b"x" * 5000

    def handler(req):
        return httpx.Response(200, content=big)

    client = mock_client(handler, fast_http(max_response_bytes=1000))
    with pytest.raises(ResponseTooLarge):
        client.fetch("https://a.example/", ANY)


def test_dns_rebinding_blocked_at_connect(monkeypatch):
    """Resolution happens at connect time and the checked IP is the one dialed."""
    calls = []

    def fake_getaddrinfo(host, port, *a, **k):
        calls.append(host)
        return [(2, 1, 6, "", ("127.0.0.1", port))]  # attacker DNS now answers loopback

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    backend = GuardedBackend()
    with pytest.raises(BlockedURL):
        backend.connect_tcp("rebind.example", 443)
    assert calls == ["rebind.example"]


def test_mixed_dns_records_rejected(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda h, p, *a, **k: [(2, 1, 6, "", ("93.184.216.34", p)), (2, 1, 6, "", ("10.0.0.1", p))],
    )
    with pytest.raises(BlockedURL):
        GuardedBackend().connect_tcp("mixed.example", 443)


def test_guarded_backend_dials_validated_ip(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", ("93.184.216.34", p))]
    )
    dialed = []
    monkeypatch.setattr(
        httpcore.SyncBackend,
        "connect_tcp",
        lambda self, host, port, *a, **k: dialed.append(host) or "stream",
    )
    assert GuardedBackend().connect_tcp("example.com", 443) == "stream"
    assert dialed == ["93.184.216.34"]


def test_real_transport_has_guard_installed_and_ignores_env_proxies(monkeypatch):
    t = build_transport()
    assert isinstance(t._pool._network_backend, GuardedBackend)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    c = HttpClient(fast_http())
    assert c._client._trust_env is False
    c.close()


def test_end_to_end_private_resolution_refused(monkeypatch):
    """Through the real transport: a public-looking name resolving to loopback is refused."""
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda h, p, *a, **k: [(2, 1, 6, "", ("127.0.0.1", p))]
    )
    with HttpClient(fast_http(max_retries=0)) as c, pytest.raises(BlockedURL):
        c.fetch("https://looks-public.example/", ANY)
