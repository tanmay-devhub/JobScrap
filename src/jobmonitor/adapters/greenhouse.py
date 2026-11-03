"""Greenhouse Job Board API (public, unauthenticated GET endpoints).

Docs: https://developers.greenhouse.io/job-board.html
  GET https://boards-api.greenhouse.io/v1/boards/{board}            -> {"name", "content"}
  GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs[?content=true]
Verified live: jobs carry `first_published` (original publication) and `updated_at`.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit

from ..enums import ATSKind, ScanOutcome
from ..http_client import HttpClient
from ..models import RawJob
from ..netguard import SourcePolicy
from ..security import html_to_text
from ..timeutil import parse_iso
from .base import ATSAdapter, BoardSnapshot, valid_board

API = "https://boards-api.greenhouse.io/v1/boards"
_PATH_RE = re.compile(r"^/(?:embed/)?([A-Za-z0-9_.\-]+)")


class GreenhouseAdapter(ATSAdapter):
    kind = ATSKind.GREENHOUSE
    policy = SourcePolicy("greenhouse", allowed_hosts=("boards-api.greenhouse.io",))

    def board_from_url(self, url: str) -> str | None:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        host = (parts.hostname or "").lower()
        if host not in (
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "boards-api.greenhouse.io",
            "job-boards.eu.greenhouse.io",
            "boards.eu.greenhouse.io",
        ):
            return None
        q = parse_qs(parts.query)
        if "for" in q:
            board = q["for"][0].lower()
        elif host == "boards-api.greenhouse.io":
            m = re.match(r"^/v1/boards/([A-Za-z0-9_.\-]+)", parts.path)
            board = m.group(1).lower() if m else None
        else:
            m = _PATH_RE.match(parts.path)
            board = m.group(1).lower() if m else None
            if board in ("embed", "v1", "jobs"):
                board = None
        return board if valid_board(board) else None

    def board_name(self, client: HttpClient, board: str) -> str | None:
        res = client.fetch(f"{API}/{board}", self.policy, allow_statuses=frozenset({200, 404}))
        if res.status != 200:
            return None
        return (res.json() or {}).get("name")

    def fetch(self, client, board, etag, with_descriptions) -> BoardSnapshot:
        url = f"{API}/{board}/jobs" + ("?content=true" if with_descriptions else "")
        res = client.fetch(
            url, self.policy, headers=self._conditional(etag), allow_statuses=frozenset({200, 404})
        )
        if res.status == 304:
            return BoardSnapshot(ScanOutcome.SUCCESS, etag=etag, not_modified=True, pages=1)
        if res.status == 404:
            return BoardSnapshot(ScanOutcome.NOT_FOUND, error="board not found")
        jobs = self.parse(res.json(), board, with_descriptions)
        return self._finish(jobs, res.headers.get("etag"))

    def parse(self, data, board, with_descriptions) -> list[RawJob]:
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("unexpected greenhouse payload")
        out: list[RawJob] = []
        for j in data["jobs"]:
            if not isinstance(j, dict) or j.get("id") is None or not j.get("title"):
                continue
            workplace = None
            for md in j.get("metadata") or []:
                if isinstance(md, dict) and str(md.get("name", "")).strip().lower() in (
                    "location type",
                    "workplace type",
                    "remote type",
                    "work type",
                ):
                    val = md.get("value")
                    workplace = val if isinstance(val, str) else None
            offices = [
                o.get("name")
                for o in j.get("offices") or []
                if isinstance(o, dict) and o.get("name")
            ]
            desc = None
            if with_descriptions and j.get("content"):
                desc = html_to_text(html.unescape(str(j["content"])))
            out.append(
                RawJob(
                    source_job_id=str(j["id"]),
                    title=str(j["title"]).strip(),
                    job_url=j.get("absolute_url"),
                    apply_url=j.get("absolute_url"),
                    location=((j.get("location") or {}).get("name") or "").strip(),
                    extra_locations=offices,
                    workplace_type=workplace,
                    department=", ".join(
                        d["name"]
                        for d in j.get("departments") or []
                        if isinstance(d, dict) and d.get("name")
                    )
                    or None,
                    published_at=parse_iso(j.get("first_published")),
                    updated_at=parse_iso(j.get("updated_at")),
                    description_text=desc,
                )
            )
        return out
