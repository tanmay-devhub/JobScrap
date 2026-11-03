"""Credential-free, network-free demo: synthetic ATS payloads served by httpx.MockTransport.

Simulates two daily collections: day 1 establishes baselines, day 2 adds/removes jobs so the
NewJobs / LocationUnclear / closing logic is visible in the exported CSVs.
All company names and domains are fictional (.example).
"""

from __future__ import annotations

from datetime import timedelta

import httpx

from .config import AppConfig, EnvSettings
from .db import Store
from .enums import ATSKind
from .http_client import HttpClient
from .models import DiscoveredCompany
from .pipeline import Context, collect, publish
from .timeutil import iso, utcnow


def demo_payloads(day: int, now) -> dict[str, object]:
    recent = iso(now - timedelta(hours=20))
    old = iso(now - timedelta(days=60))
    gh_jobs = [
        {
            "id": 1001,
            "title": "Backend Engineer",
            "absolute_url": "https://job-boards.greenhouse.io/acmerobotics/jobs/1001",
            "location": {"name": "San Francisco, CA"},
            "first_published": old,
            "updated_at": recent,
            "metadata": [{"name": "Location Type", "value": "On-Site"}],
        },
        {
            "id": 1002,
            "title": '=HYPERLINK("http://evil.example","Click")',
            "absolute_url": "https://job-boards.greenhouse.io/acmerobotics/jobs/1002",
            "location": {"name": "Remote - US"},
            "first_published": old,
            "updated_at": old,
        },
        {
            "id": 1003,
            "title": "Field Engineer",
            "absolute_url": "https://job-boards.greenhouse.io/acmerobotics/jobs/1003",
            "location": {"name": "Berlin, Germany"},
            "first_published": old,
            "updated_at": old,
        },
    ]
    if day >= 2:
        gh_jobs.append(
            {
                "id": 1004,
                "title": "Staff ML Engineer",
                "absolute_url": "https://job-boards.greenhouse.io/acmerobotics/jobs/1004",
                "location": {"name": "Hybrid - New York, NY"},
                "first_published": recent,
                "updated_at": recent,
            }
        )
    lever = [
        {
            "id": "a1",
            "text": "Product Designer",
            "hostedUrl": "https://jobs.lever.co/betacloud/a1",
            "applyUrl": "https://jobs.lever.co/betacloud/a1/apply",
            "createdAt": int((now - timedelta(days=40)).timestamp() * 1000),
            "workplaceType": "remote",
            "categories": {"location": "Remote", "allLocations": ["Remote"]},
        },
        {
            "id": "a2",
            "text": "Account Executive",
            "hostedUrl": "https://jobs.lever.co/betacloud/a2",
            "createdAt": int((now - timedelta(days=2)).timestamp() * 1000),
            "workplaceType": "hybrid",
            "country": "US",
            "categories": {"location": "Austin, TX", "allLocations": ["Austin, TX"]},
        },
    ]
    if day >= 2:
        lever = lever[1:]  # a1 disappears (closed after the confirmation threshold)
        lever.append(
            {
                "id": "a3",
                "text": "Support Lead",
                "hostedUrl": "https://jobs.lever.co/betacloud/a3",
                "workplaceType": "remote",
                "categories": {"location": "Remote - Americas"},
            }
        )
    ashby = {
        "apiVersion": "1",
        "jobs": [
            {
                "id": "x1",
                "title": "Founding Engineer",
                "jobUrl": "https://jobs.ashbyhq.com/gammaai/x1",
                "applyUrl": "https://jobs.ashbyhq.com/gammaai/x1/application",
                "location": "Remote (Worldwide)",
                "workplaceType": "Remote",
                "isListed": True,
                "publishedAt": old,
            },
            {
                "id": "x2",
                "title": "Ops Manager",
                "jobUrl": "https://jobs.ashbyhq.com/gammaai/x2",
                "location": "Seattle, WA",
                "workplaceType": "OnSite",
                "isListed": True,
                "publishedAt": old,
                "address": {"postalAddress": {"addressCountry": "USA"}},
            },
        ],
    }
    return {
        "https://boards-api.greenhouse.io/v1/boards/acmerobotics/jobs": {
            "jobs": gh_jobs,
            "meta": {"total": len(gh_jobs)},
        },
        "https://api.lever.co/v0/postings/betacloud": lever,
        "https://api.ashbyhq.com/posting-api/job-board/gammaai": ashby,
    }


def mock_transport(payloads: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url.copy_with(query=None))
        if key in payloads:
            data = payloads[key]
            if isinstance(data, list) and request.url.params.get("skip", "0") != "0":
                data = []
            return httpx.Response(200, json=data)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def run_demo(out_dir: str) -> dict:
    cfg = AppConfig()
    cfg.publish.csv_dir = f"{out_dir}/exports"
    cfg.http.per_host_min_interval_seconds = 0
    store = Store(f"{out_dir}/demo.sqlite3")
    env = EnvSettings(db_path=f"{out_dir}/demo.sqlite3")
    day1 = utcnow() - timedelta(days=1)
    for name, domain, ats, board in [
        ("Acme Robotics", "acmerobotics.example", ATSKind.GREENHOUSE, "acmerobotics"),
        ("BetaCloud", "betacloud.example", ATSKind.LEVER, "betacloud"),
        ("Gamma AI", "gammaai.example", ATSKind.ASHBY, "gammaai"),
    ]:
        store.upsert_company(
            DiscoveredCompany(name=name, domain=domain, ats=ats, board=board, source="demo"), day1
        )

    results = {}
    for day, when in ((1, day1), (2, utcnow())):
        payloads = demo_payloads(day, when)
        ctx = Context(
            cfg,
            env,
            store,
            client_factory=lambda p=payloads: HttpClient(cfg.http, transport=mock_transport(p)),
            now=lambda w=when: w,
        )
        results[f"collect_day{day}"] = collect(ctx)
        # Demo shortcut: on day 2, require only one missing scan so the removal is visible.
        cfg.freshness.close_after_missing_scans = 1
    ctx = Context(cfg, env, store, now=utcnow)
    results["publish"] = publish(ctx, force=True, sheets=False)
    store.close()
    return results
