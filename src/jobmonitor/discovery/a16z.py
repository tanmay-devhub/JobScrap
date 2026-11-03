"""a16z portfolio (https://a16z.com/portfolio/).

Verified: the page embeds the full portfolio as a JSON array assigned to
`window.a16z_portfolio_companies`; we decode only that JSON literal (never execute script).
robots.txt allows all paths. Stage values observed: Seed, Venture, Growth, M&A, IPO, SPAC, DPO, EXIT.
"""

from __future__ import annotations

from ..enums import CompanyStatus
from ..models import DiscoveredCompany
from ..netguard import SourcePolicy
from .base import DiscoveryResult, DiscoverySource, json_array_after

URL = "https://a16z.com/portfolio/"
MARKER = "window.a16z_portfolio_companies"
EXIT_STATUS = {
    "m&a": CompanyStatus.ACQUIRED,
    "ipo": CompanyStatus.PUBLIC,
    "spac": CompanyStatus.PUBLIC,
    "dpo": CompanyStatus.PUBLIC,
}


class A16ZSource(DiscoverySource):
    name = "a16z"
    policy = SourcePolicy(
        "a16z", allowed_hosts=("a16z.com",), check_robots=True, max_bytes=20 * 1024 * 1024
    )

    def fetch(self, client, cfg) -> DiscoveryResult:
        res = client.fetch(URL, self.policy)
        return DiscoveryResult(self.name, self.parse(res.text, cfg))

    def parse(self, payload, cfg) -> list[DiscoveredCompany]:
        arr = json_array_after(payload, MARKER)
        out = []
        for c in arr:
            if not isinstance(c, dict) or not c.get("title"):
                continue
            stages = [str(s).lower() for s in c.get("stage") or [] if s]
            status, evidence = CompanyStatus.UNKNOWN, None
            for s in stages:
                if s in EXIT_STATUS:
                    status, evidence = EXIT_STATUS[s], f"a16z stage={s}"
                    break
            if status == CompanyStatus.UNKNOWN and (c.get("exit_date") or c.get("acquirer")):
                status, evidence = CompanyStatus.ACQUIRED, "a16z exit_date/acquirer present"
            out.append(
                DiscoveredCompany(
                    name=str(c["title"]).strip(),
                    website=c.get("web") or None,
                    domain=c.get("web") or None,
                    status=status,
                    status_evidence=evidence,
                    source=self.name,
                    source_ref=str(c.get("id") or ""),
                    batch=",".join(stages) or None,
                )
            )
        return out
