from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..config import DiscoveryConfig
from ..http_client import FetchError, HttpClient
from ..models import DiscoveredCompany
from ..netguard import BlockedURL, SourcePolicy


@dataclass
class DiscoveryResult:
    source: str
    companies: list[DiscoveredCompany] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    note: str | None = None  # e.g. coverage limitation


class DiscoverySource(ABC):
    name: str
    policy: SourcePolicy

    @abstractmethod
    def fetch(self, client: HttpClient, cfg: DiscoveryConfig) -> DiscoveryResult: ...

    @abstractmethod
    def parse(self, payload, cfg: DiscoveryConfig) -> list[DiscoveredCompany]:
        """Pure parser (fixture-testable)."""

    def discover(self, client: HttpClient, cfg: DiscoveryConfig) -> DiscoveryResult:
        try:
            res = self.fetch(client, cfg)
        except (FetchError, BlockedURL) as e:
            return DiscoveryResult(self.name, ok=False, error=f"{type(e).__name__}: {e}")
        except (ValueError, KeyError, TypeError) as e:
            return DiscoveryResult(
                self.name, ok=False, error=f"parse error: {type(e).__name__}: {e}"
            )
        res.companies = res.companies[: cfg.max_companies_per_source]
        return res


def json_array_after(text: str, marker: str) -> list:
    """Decode the JSON array that follows `marker` in `text`. JSON only - nothing is executed."""
    i = text.index(marker)
    j = text.index("[", i + len(marker))
    arr, _ = json.JSONDecoder().raw_decode(text, j)
    if not isinstance(arr, list):
        raise ValueError("expected JSON array")
    return arr
