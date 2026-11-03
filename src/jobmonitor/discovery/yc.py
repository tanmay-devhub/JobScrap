"""Y Combinator companies via the open yc-oss/api dataset (https://github.com/yc-oss/api).

The dataset is a daily-refreshed static JSON mirror of the public YC directory, served from
GitHub Pages; no keys are involved. Verified fields: name, slug, website, batch, status
(Active/Inactive/Acquired/Public), isHiring, url.
  https://yc-oss.github.io/api/companies/hiring.json     (default: companies currently hiring)
  https://yc-oss.github.io/api/batches/{batch-slug}.json (e.g. summer-2025)
"""

from __future__ import annotations

import re

from ..config import DiscoveryConfig
from ..enums import CompanyStatus
from ..models import DiscoveredCompany
from ..netguard import SourcePolicy
from .base import DiscoveryResult, DiscoverySource

BASE = "https://yc-oss.github.io/api"
STATUS = {
    "active": CompanyStatus.ACTIVE,
    "inactive": CompanyStatus.INACTIVE,
    "acquired": CompanyStatus.ACQUIRED,
    "public": CompanyStatus.PUBLIC,
}
_BATCH_RE = re.compile(r"^[a-z]+-\d{4}$")


class YCSource(DiscoverySource):
    name = "yc"
    policy = SourcePolicy("yc", allowed_hosts=("yc-oss.github.io",), max_bytes=40 * 1024 * 1024)

    def urls(self, cfg: DiscoveryConfig) -> list[str]:
        if cfg.yc_batches:
            slugs = [b.strip().lower().replace(" ", "-") for b in cfg.yc_batches]
            return [f"{BASE}/batches/{s}.json" for s in slugs if _BATCH_RE.match(s)]
        return [f"{BASE}/companies/hiring.json"]

    def fetch(self, client, cfg) -> DiscoveryResult:
        companies: list[DiscoveredCompany] = []
        for url in self.urls(cfg):
            companies.extend(self.parse(client.fetch(url, self.policy).json(), cfg))
        return DiscoveryResult(self.name, companies)

    def parse(self, payload, cfg) -> list[DiscoveredCompany]:
        if not isinstance(payload, list):
            raise ValueError("unexpected yc payload")
        out = []
        for c in payload:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            raw_status = str(c.get("status") or "").strip().lower()
            status = STATUS.get(raw_status, CompanyStatus.UNKNOWN)
            out.append(
                DiscoveredCompany(
                    name=str(c["name"]).strip(),
                    website=c.get("website") or None,
                    domain=c.get("website") or None,
                    status=status,
                    status_evidence=f"yc directory status={c.get('status')}"
                    if raw_status
                    else None,
                    source=self.name,
                    source_ref=str(c.get("slug") or c.get("id") or ""),
                    batch=c.get("batch"),
                )
            )
        return out
