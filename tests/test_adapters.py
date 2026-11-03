import httpx

from conftest import load, mock_client
from jobmonitor.adapters import ADAPTERS, board_from_url
from jobmonitor.adapters.lever import PAGE_SIZE
from jobmonitor.enums import ATSKind, ScanOutcome

GH, LV, AB = (ADAPTERS[k] for k in (ATSKind.GREENHOUSE, ATSKind.LEVER, ATSKind.ASHBY))


def test_greenhouse_parse():
    jobs = GH.parse(load("greenhouse_jobs.json"), "examplerobotics", False)
    assert [j.source_job_id for j in jobs] == ["4001", "4002", "4003", "4004"]  # broken row skipped
    j = jobs[0]
    assert j.workplace_type == "On-Site"
    assert j.published_at.isoformat() == "2026-09-01T13:00:00+00:00"  # first_published, UTC
    assert j.updated_at.isoformat() == "2026-10-20T14:00:00+00:00"
    assert jobs[3].published_at is None


def test_lever_parse():
    jobs = LV.parse(load("lever_postings.json"), "examplecloud", False)
    assert len(jobs) == 3
    assert jobs[0].country_codes == ["US"]
    assert jobs[0].extra_locations == ["Denver, Colorado"]
    assert jobs[0].published_at is not None
    assert jobs[2].workplace_type is None  # "unspecified" is not a signal
    # untrusted description is kept only as inert text
    assert "<script>" in jobs[1].description_text


def test_ashby_parse_skips_unlisted_and_maps_country():
    jobs = AB.parse(load("ashby_board.json"), "exampleai", False)
    assert [j.title for j in jobs] == ["Security Engineer, Cloud", "Recruiter"]
    assert jobs[0].country_codes == ["Canada", "Canada", "US"]
    assert jobs[1].workplace_type == "Remote"  # derived from isRemote


def _router(routes):
    def handler(req: httpx.Request):
        for (path, skip), resp in routes.items():
            if req.url.path == path and (skip is None or req.url.params.get("skip") == str(skip)):
                return resp(req) if callable(resp) else resp
        return httpx.Response(404, json={"ok": False})

    return handler


def _lever_page(n, start=0):
    return [
        {
            "id": f"id-{start + i}",
            "text": f"Job {start + i}",
            "categories": {"location": "Austin, TX"},
        }
        for i in range(n)
    ]


def test_lever_pagination_complete():
    path = "/v0/postings/examplecloud"
    client = mock_client(
        _router(
            {
                (path, 0): httpx.Response(200, json=_lever_page(PAGE_SIZE)),
                (path, PAGE_SIZE): httpx.Response(200, json=_lever_page(3, PAGE_SIZE)),
            }
        )
    )
    snap = LV.scan(client, "examplecloud")
    assert (
        snap.outcome == ScanOutcome.SUCCESS and snap.pages == 2 and len(snap.jobs) == PAGE_SIZE + 3
    )


def test_lever_second_page_failure_is_partial():
    path = "/v0/postings/examplecloud"
    client = mock_client(
        _router(
            {
                (path, 0): httpx.Response(200, json=_lever_page(PAGE_SIZE)),
                (path, PAGE_SIZE): httpx.Response(500),
            }
        )
    )
    snap = LV.scan(client, "examplecloud")
    assert snap.outcome == ScanOutcome.PARTIAL and not snap.complete


def test_not_found_zero_jobs_and_errors_are_distinct():
    client = mock_client(
        _router(
            {
                ("/v1/boards/empty/jobs", None): httpx.Response(200, json={"jobs": [], "meta": {}}),
                ("/v1/boards/broken/jobs", None): httpx.Response(200, content=b"<html>not json"),
                ("/v1/boards/down/jobs", None): httpx.Response(503),
            }
        )
    )
    assert GH.scan(client, "empty").outcome == ScanOutcome.ZERO_JOBS
    assert GH.scan(client, "missing").outcome == ScanOutcome.NOT_FOUND
    assert GH.scan(client, "broken").outcome == ScanOutcome.SOURCE_ERROR
    assert GH.scan(client, "down").outcome == ScanOutcome.SOURCE_ERROR
    assert GH.scan(client, "../etc").outcome == ScanOutcome.BLOCKED  # identifier validation


def test_conditional_request_304():
    seen = {}

    def handler(req):
        seen["inm"] = req.headers.get("if-none-match")
        return httpx.Response(304)

    snap = AB.scan(mock_client(handler), "exampleai", etag='W/"abc"')
    assert seen["inm"] == 'W/"abc"'
    assert snap.outcome == ScanOutcome.SUCCESS and snap.not_modified and snap.complete


def test_board_from_url():
    assert board_from_url("https://boards.greenhouse.io/embed/job_board?for=Acme") == (
        ATSKind.GREENHOUSE,
        "acme",
    )
    assert board_from_url("https://job-boards.greenhouse.io/acme/jobs/123") == (
        ATSKind.GREENHOUSE,
        "acme",
    )
    assert board_from_url("https://jobs.lever.co/Acme-Co/abc") == (ATSKind.LEVER, "acme-co")
    assert board_from_url("https://jobs.ashbyhq.com/acme.ai/123") == (ATSKind.ASHBY, "acme.ai")
    assert board_from_url("https://jobs.ashbyhq.com/acme%20ai") is None
    assert board_from_url("https://evil.example/boards.greenhouse.io/acme") is None
