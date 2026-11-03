import httpx

from conftest import load, mock_client
from jobmonitor.config import DiscoveryConfig
from jobmonitor.db import canonical_domain, company_id_for
from jobmonitor.discovery.a16z import A16ZSource
from jobmonitor.discovery.resolve import find_ats_links, resolve_company
from jobmonitor.discovery.techstars import TechstarsSource
from jobmonitor.discovery.yc import YCSource
from jobmonitor.enums import ATSKind, CompanyStatus
from jobmonitor.models import DiscoveredCompany

CFG = DiscoveryConfig()


def test_yc_parse_status_evidence():
    cos = {c.name: c for c in YCSource().parse(load("yc_companies.json"), CFG)}
    assert set(cos) == {"Example Robotics", "Gone Inc", "Bought Co", "Mystery"}
    assert cos["Example Robotics"].status == CompanyStatus.ACTIVE
    assert cos["Gone Inc"].status == CompanyStatus.INACTIVE
    assert cos["Bought Co"].status == CompanyStatus.ACQUIRED
    assert cos["Mystery"].status == CompanyStatus.UNKNOWN and cos["Mystery"].status_evidence is None
    assert cos["Example Robotics"].batch == "Summer 2025"


def test_yc_batch_urls_validated():
    urls = YCSource().urls(DiscoveryConfig(yc_batches=["Summer 2025", "../../etc"]))
    assert urls == ["https://yc-oss.github.io/api/batches/summer-2025.json"]


def test_a16z_parse_json_literal_only():
    cos = {c.name: c for c in A16ZSource().parse(load("a16z_portfolio.html"), CFG)}
    assert "Decoy" not in cos
    assert cos["Example Venture"].status == CompanyStatus.UNKNOWN
    assert cos["Old Exit"].status == CompanyStatus.ACQUIRED
    assert cos["Listed Co"].status == CompanyStatus.PUBLIC
    assert cos['Seed "Quoted" Co </script-ish>'].website is None


def test_techstars_flight_payload():
    cos = TechstarsSource().parse(load("techstars_portfolio.html"), CFG)
    assert [c.name for c in cos] == ["Example ID", "Fit Pass", "Big Public"]  # deduped
    assert [c.status for c in cos] == [
        CompanyStatus.ACTIVE,
        CompanyStatus.ACQUIRED,
        CompanyStatus.PUBLIC,
    ]


def test_discovery_failure_is_isolated():
    client = mock_client(lambda req: httpx.Response(500))
    res = YCSource().discover(client, CFG)
    assert not res.ok and "HTTP 500" in res.error


def test_canonical_domain_dedup(store):
    assert canonical_domain("https://WWW.Example.com:443/path") == "example.com"
    assert canonical_domain("example.com") == "example.com"
    assert canonical_domain("not a domain") is None
    a = store.upsert_company(
        DiscoveredCompany(
            name="Ex", website="https://www.example.com", source="yc", source_ref="ex"
        )
    )
    b = store.upsert_company(
        DiscoveredCompany(
            name="Example Inc",
            website="http://example.com/",
            source="a16z",
            source_ref="9",
            status=CompanyStatus.ACQUIRED,
            status_evidence="a16z",
        )
    )
    assert a == b == "example.com"
    assert store.one("SELECT count(*) n FROM companies")["n"] == 1
    prov = {r["source"] for r in store.q("SELECT source FROM company_sources")}
    assert prov == {"yc", "a16z"}  # provenance preserved
    assert store.one("SELECT status FROM companies")["status"] == "acquired"
    # later unknown status does not erase evidence
    store.upsert_company(DiscoveredCompany(name="Ex", website="example.com", source="techstars"))
    assert store.one("SELECT status FROM companies")["status"] == "acquired"
    assert company_id_for("No Site", None) == "name:no-site"


def test_inactive_companies_not_scanned_or_probed(store):
    store.upsert_company(
        DiscoveredCompany(
            name="Dead",
            domain="dead.example",
            status=CompanyStatus.INACTIVE,
            ats=ATSKind.LEVER,
            board="dead",
            source="yc",
        )
    )
    store.upsert_company(DiscoveredCompany(name="Unknown", domain="unk.example", source="yc"))
    assert store.boards_to_scan(10) == []
    assert [r["name"] for r in store.companies_needing_probe(10)] == ["Unknown"]


def test_find_ats_links_from_company_page():
    boards, careers = find_ats_links(
        load("company_home.html"), "https://www.examplerobotics.example/"
    )
    assert (ATSKind.GREENHOUSE, "examplerobotics") in boards
    assert (ATSKind.LEVER, "examplecloud") in boards
    assert all(b[0] != ATSKind.ASHBY for b in boards)  # invalid id rejected
    assert careers == ["https://www.examplerobotics.example/careers"]


def test_resolve_prefers_site_link_and_confirms_board():
    def handler(req):
        if req.url.host == "www.examplerobotics.example":
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /")
            return httpx.Response(200, text=load("company_home.html"))
        if (
            req.url.host == "boards-api.greenhouse.io"
            and req.url.path == "/v1/boards/examplerobotics/jobs"
        ):
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(404)

    found = resolve_company(
        mock_client(handler), "Example Robotics", "https://www.examplerobotics.example/"
    )
    assert found == (ATSKind.GREENHOUSE, "examplerobotics", "site-link")


def test_resolve_respects_robots_and_requires_name_match_for_guesses():
    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        if req.url.host == "boards-api.greenhouse.io":
            if req.url.path == "/v1/boards/acme":
                return httpx.Response(200, json={"name": "Totally Different Corp"})
            if req.url.path == "/v1/boards/acmerockets":
                return httpx.Response(200, json={"name": "Acme Rockets, Inc."})
        return httpx.Response(404)

    client = mock_client(handler)
    assert resolve_company(client, "Acme", "https://acme.example/") is None  # name mismatch
    assert resolve_company(client, "Acme Rockets", "https://acme.example/") == (
        ATSKind.GREENHOUSE,
        "acmerockets",
        "greenhouse-name-match",
    )
    assert client.stats.blocked >= 1  # homepage disallowed by robots.txt
