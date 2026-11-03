"""Ashby public job posting API.

Docs: https://developers.ashbyhq.com/docs/public-job-posting-api
  GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=false
Verified live: `publishedAt`, `workplaceType`, `isRemote`, `isListed`, structured
`address.postalAddress.addressCountry` for primary and secondary locations.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from ..enums import ATSKind, ScanOutcome
from ..models import RawJob
from ..netguard import SourcePolicy
from ..timeutil import parse_iso
from .base import ATSAdapter, BoardSnapshot, valid_board

API = "https://api.ashbyhq.com/posting-api/job-board"
_US_COUNTRY = {"usa", "us", "united states", "united states of america"}


def _country(addr) -> str | None:
    if not isinstance(addr, dict):
        return None
    c = (addr.get("postalAddress") or {}).get("addressCountry")
    if not isinstance(c, str) or not c.strip():
        return None
    return "US" if c.strip().lower() in _US_COUNTRY else c.strip()


class AshbyAdapter(ATSAdapter):
    kind = ATSKind.ASHBY
    policy = SourcePolicy("ashby", allowed_hosts=("api.ashbyhq.com",))

    def board_from_url(self, url: str) -> str | None:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        if host == "jobs.ashbyhq.com":
            m = re.match(r"^/([^/?#]+)", parts.path)
        elif host == "api.ashbyhq.com":
            m = re.match(r"^/posting-api/job-board/([^/?#]+)", parts.path)
        else:
            return None
        board = unquote(m.group(1)).lower() if m else None
        return board if valid_board(board) else None

    def fetch(self, client, board, etag, with_descriptions) -> BoardSnapshot:
        res = client.fetch(
            f"{API}/{board}?includeCompensation=false",
            self.policy,
            headers=self._conditional(etag),
            allow_statuses=frozenset({200, 404}),
        )
        if res.status == 304:
            return BoardSnapshot(ScanOutcome.SUCCESS, etag=etag, not_modified=True)
        if res.status == 404:
            return BoardSnapshot(ScanOutcome.NOT_FOUND, error="board not found")
        return self._finish(
            self.parse(res.json(), board, with_descriptions), res.headers.get("etag")
        )

    def parse(self, data, board, with_descriptions) -> list[RawJob]:
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("unexpected ashby payload")
        out = []
        for j in data["jobs"]:
            if not isinstance(j, dict) or not j.get("id") or not j.get("title"):
                continue
            if j.get("isListed") is False:
                continue
            secondary = [s for s in j.get("secondaryLocations") or [] if isinstance(s, dict)]
            countries = [
                c
                for c in [
                    _country(j.get("address")),
                    *(_country(s.get("address")) for s in secondary),
                ]
                if c
            ]
            workplace = j.get("workplaceType")
            if not workplace and j.get("isRemote") is True:
                workplace = "Remote"
            desc = j.get("descriptionPlain") if isinstance(j.get("descriptionPlain"), str) else None
            out.append(
                RawJob(
                    source_job_id=str(j["id"]),
                    title=str(j["title"]).strip(),
                    job_url=j.get("jobUrl"),
                    apply_url=j.get("applyUrl") or j.get("jobUrl"),
                    location=(j.get("location") or "").strip(),
                    extra_locations=[
                        s["location"] for s in secondary if isinstance(s.get("location"), str)
                    ],
                    workplace_type=workplace,
                    country_codes=countries,
                    department=j.get("department"),
                    team=j.get("team"),
                    employment_type=j.get("employmentType"),
                    published_at=parse_iso(j.get("publishedAt")),
                    description_text=desc[:4000] if desc else None,
                )
            )
        return out
