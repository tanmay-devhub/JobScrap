"""Techstars portfolio (https://www.techstars.com/portfolio) - PARTIAL coverage.

Verified: the server-rendered page contains only a featured subset (`portfolioUnicorns`,
~30 records with stage PRIVATE / ACQUIRED / PUBLIC) inside the Next.js flight payload
(`self.__next_f.push([1, "<json text>"])`). The full ~5k-company directory is queried
client-side through a search service with an embedded key; using that key would mean
scraping third-party credentials, so it is deliberately NOT used.
robots.txt: "User-Agent: * / Allow: /".
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from ..enums import CompanyStatus
from ..models import DiscoveredCompany
from ..netguard import SourcePolicy
from .base import DiscoveryResult, DiscoverySource

URL = "https://www.techstars.com/portfolio"
PUSH_RE = re.compile(r"self\.__next_f\.push\(")
STATUS = {
    "private": CompanyStatus.ACTIVE,
    "acquired": CompanyStatus.ACQUIRED,
    "public": CompanyStatus.PUBLIC,
    "inactive": CompanyStatus.INACTIVE,
    "closed": CompanyStatus.INACTIVE,
}


def flight_text(html: str) -> str:
    """Concatenate Next.js flight chunks; each chunk is decoded as JSON, never evaluated."""
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    decoder = json.JSONDecoder()
    for script in soup.find_all("script"):
        text = script.get_text()
        for m in PUSH_RE.finditer(text):
            try:
                val, _ = decoder.raw_decode(text, m.end())
            except json.JSONDecodeError:
                continue
            if isinstance(val, list) and len(val) >= 2 and isinstance(val[1], str):
                parts.append(val[1])
    return "".join(parts)


class TechstarsSource(DiscoverySource):
    name = "techstars"
    policy = SourcePolicy(
        "techstars",
        allowed_hosts=("www.techstars.com",),
        check_robots=True,
        max_bytes=10 * 1024 * 1024,
    )

    def fetch(self, client, cfg) -> DiscoveryResult:
        res = client.fetch(URL, self.policy)
        return DiscoveryResult(
            self.name,
            self.parse(res.text, cfg),
            note="partial: server-rendered featured companies only",
        )

    def parse(self, payload, cfg) -> list[DiscoveredCompany]:
        text = flight_text(payload)
        out: list[DiscoveredCompany] = []
        seen: set[str] = set()
        decoder = json.JSONDecoder()
        for m in re.finditer(r'"portfolio(?:Unicorns|Companies)"\s*:\s*\[', text):
            arr, _ = decoder.raw_decode(text, m.end() - 1)
            for c in arr if isinstance(arr, list) else []:
                if not isinstance(c, dict) or not c.get("name") or c.get("id") in seen:
                    continue
                seen.add(c.get("id"))
                stage = str(c.get("stage") or "").lower()
                status = STATUS.get(stage, CompanyStatus.UNKNOWN)
                out.append(
                    DiscoveredCompany(
                        name=str(c["name"]).strip(),
                        website=c.get("website") or None,
                        domain=c.get("website") or None,
                        status=status,
                        status_evidence=f"techstars stage={c.get('stage')}" if stage else None,
                        source=self.name,
                        source_ref=str(c.get("portfolio_company_id") or c.get("id") or ""),
                        batch=str(c["session_year"]) if c.get("session_year") else None,
                    )
                )
        if not out:
            raise ValueError("no portfolio records found in techstars page")
        return out
