from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..enums import ATSKind, ScanOutcome
from ..http_client import FetchError, HttpClient
from ..models import RawJob
from ..netguard import BlockedURL, SourcePolicy

BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,99}$")


def valid_board(board: str | None) -> bool:
    return bool(board) and bool(BOARD_RE.match(board)) and ".." not in board


@dataclass
class BoardSnapshot:
    outcome: ScanOutcome
    jobs: list[RawJob] = field(default_factory=list)
    etag: str | None = None
    not_modified: bool = False
    error: str | None = None
    pages: int = 0

    @property
    def complete(self) -> bool:
        return self.outcome in (ScanOutcome.SUCCESS, ScanOutcome.ZERO_JOBS)


class ATSAdapter(ABC):
    kind: ATSKind
    policy: SourcePolicy

    @abstractmethod
    def board_from_url(self, url: str) -> str | None:
        """Extract a board identifier from a careers/job URL hosted on this ATS."""

    @abstractmethod
    def fetch(
        self, client: HttpClient, board: str, etag: str | None, with_descriptions: bool
    ) -> BoardSnapshot: ...

    @abstractmethod
    def parse(self, data, board: str, with_descriptions: bool) -> list[RawJob]:
        """Pure parser over the decoded JSON payload (fixture-testable)."""

    def scan(
        self,
        client: HttpClient,
        board: str,
        etag: str | None = None,
        with_descriptions: bool = False,
    ) -> BoardSnapshot:
        """Never raises for source problems; classifies them into a ScanOutcome."""
        if not valid_board(board):
            return BoardSnapshot(ScanOutcome.BLOCKED, error=f"invalid board identifier {board!r}")
        try:
            return self.fetch(client, board, etag, with_descriptions)
        except BlockedURL as e:
            return BoardSnapshot(ScanOutcome.BLOCKED, error=str(e))
        except FetchError as e:
            return BoardSnapshot(ScanOutcome.SOURCE_ERROR, error=str(e))
        except (ValueError, KeyError, TypeError) as e:  # malformed payload
            return BoardSnapshot(
                ScanOutcome.SOURCE_ERROR, error=f"parse error: {type(e).__name__}: {e}"
            )

    @staticmethod
    def _conditional(etag: str | None) -> dict[str, str]:
        return {"If-None-Match": etag} if etag else {}

    @staticmethod
    def _finish(jobs: list[RawJob], etag: str | None, pages: int = 1) -> BoardSnapshot:
        outcome = ScanOutcome.SUCCESS if jobs else ScanOutcome.ZERO_JOBS
        return BoardSnapshot(outcome, jobs=jobs, etag=etag, pages=pages)
