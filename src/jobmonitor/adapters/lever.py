"""Lever Postings API (public).

Docs: https://github.com/lever/postings-api
  GET https://api.lever.co/v0/postings/{board}?mode=json&skip=N&limit=M
Verified live: `createdAt` (epoch ms), `workplaceType`, `country`, `categories.allLocations`.
EU-hosted accounts (api.eu.lever.co) are not supported yet.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..enums import ATSKind, ScanOutcome
from ..http_client import FetchError
from ..models import RawJob
from ..netguard import SourcePolicy
from ..timeutil import from_epoch_ms
from .base import ATSAdapter, BoardSnapshot, valid_board

API = "https://api.lever.co/v0/postings"
PAGE_SIZE = 100
MAX_PAGES = 50


class LeverAdapter(ATSAdapter):
    kind = ATSKind.LEVER
    policy = SourcePolicy("lever", allowed_hosts=("api.lever.co",))

    def board_from_url(self, url: str) -> str | None:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        if host not in ("jobs.lever.co", "api.lever.co"):
            return None
        path = parts.path
        if host == "api.lever.co":
            path = re.sub(r"^/v0/postings", "", path)
        m = re.match(r"^/([A-Za-z0-9_.\-]+)", path)
        board = m.group(1).lower() if m else None
        return board if valid_board(board) else None

    def fetch(self, client, board, etag, with_descriptions) -> BoardSnapshot:
        jobs = []
        skip = 0
        first_etag = None
        for page in range(1, MAX_PAGES + 1):
            headers = self._conditional(etag) if page == 1 else {}
            url = f"{API}/{board}?mode=json&skip={skip}&limit={PAGE_SIZE}"
            if page == 1:
                res = client.fetch(
                    url, self.policy, headers=headers, allow_statuses=frozenset({200, 404})
                )
            else:
                try:
                    res = client.fetch(url, self.policy, allow_statuses=frozenset({200}))
                except FetchError as e:
                    return BoardSnapshot(ScanOutcome.PARTIAL, error=f"page {page}: {e}", pages=page)
            if page == 1:
                if res.status == 304:
                    return BoardSnapshot(ScanOutcome.SUCCESS, etag=etag, not_modified=True, pages=1)
                if res.status == 404:
                    return BoardSnapshot(ScanOutcome.NOT_FOUND, error="board not found")
                first_etag = res.headers.get("etag")
            data = res.json()
            jobs.extend(self.parse(data, board, with_descriptions))
            if len(data) < PAGE_SIZE:
                return self._finish(jobs, first_etag, pages=page)
            skip += PAGE_SIZE
        return BoardSnapshot(ScanOutcome.PARTIAL, error="page limit reached", pages=MAX_PAGES)

    def parse(self, data, board, with_descriptions) -> list[RawJob]:
        if not isinstance(data, list):
            raise ValueError("unexpected lever payload")
        out = []
        for j in data:
            if not isinstance(j, dict) or not j.get("id") or not j.get("text"):
                continue
            cats = j.get("categories") or {}
            locs = [loc for loc in cats.get("allLocations") or [] if isinstance(loc, str)]
            primary = cats.get("location") or (locs[0] if locs else "")
            desc = None
            if with_descriptions or j.get("descriptionPlain"):
                parts = [j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
                desc = " ".join(p for p in parts if isinstance(p, str))[:4000] or None
            country = j.get("country")
            out.append(
                RawJob(
                    source_job_id=str(j["id"]),
                    title=str(j["text"]).strip(),
                    job_url=j.get("hostedUrl"),
                    apply_url=j.get("applyUrl") or j.get("hostedUrl"),
                    location=primary.strip() if isinstance(primary, str) else "",
                    extra_locations=[x for x in locs if x != primary],
                    workplace_type=j.get("workplaceType")
                    if j.get("workplaceType") != "unspecified"
                    else None,
                    country_codes=[country] if isinstance(country, str) and country else [],
                    department=cats.get("department"),
                    team=cats.get("team"),
                    employment_type=cats.get("commitment"),
                    published_at=from_epoch_ms(j.get("createdAt")),
                    description_text=desc,
                )
            )
        return out
