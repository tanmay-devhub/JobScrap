"""Resolve a company's real ATS board identifier. Names are never assumed to be identifiers.

Evidence, strongest first:
1. `site-link`: the company's own homepage or careers page links/embeds a Greenhouse, Lever,
   or Ashby board (robots.txt respected); the board is then confirmed to exist via its API.
2. `greenhouse-name-match`: a slug guess whose Greenhouse board metadata `name` equals the
   company name (normalized). Lever/Ashby expose no board-level name, so they are never guessed.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..adapters import ADAPTERS, board_from_url
from ..adapters.greenhouse import GreenhouseAdapter
from ..db import canonical_domain, slugify
from ..enums import ATSKind
from ..http_client import FetchError, HttpClient
from ..netguard import BlockedURL, SourcePolicy

log = logging.getLogger(__name__)

SITE_POLICY = SourcePolicy("company-site", check_robots=True, max_bytes=3 * 1024 * 1024)
ATS_URL_RE = re.compile(
    r"https?://(?:boards|job-boards|boards-api)(?:\.eu)?\.greenhouse\.io/[^\s\"'<>)]+"
    r"|https?://jobs\.lever\.co/[^\s\"'<>)]+"
    r"|https?://jobs\.ashbyhq\.com/[^\s\"'<>)]+",
    re.I,
)
CAREER_HINT = re.compile(r"\b(careers?|jobs|join(-us| us)?|work-with-us|hiring|open-roles)\b", re.I)


def _norm_name(name: str) -> str:
    name = re.sub(r"\b(inc|llc|ltd|corp|co|labs?|technologies|hq|ai)\b\.?", "", name.lower())
    return re.sub(r"[^a-z0-9]", "", name)


def find_ats_links(html: str, base_url: str) -> tuple[list[tuple[ATSKind, str]], list[str]]:
    """Return (ATS boards referenced, same-site career page candidates)."""
    soup = BeautifulSoup(html, "html.parser")
    boards: list[tuple[ATSKind, str]] = []
    careers: list[str] = []
    site = canonical_domain(base_url)
    urls = [
        el.get(attr)
        for el in soup.find_all(["a", "iframe", "script"])
        for attr in ("href", "src")
        if el.get(attr)
    ]
    urls += ATS_URL_RE.findall(html)
    for u in urls:
        absolute = urljoin(base_url, u.strip())
        found = board_from_url(absolute)
        if found and found not in boards:
            boards.append(found)
            continue
        if canonical_domain(absolute) == site and CAREER_HINT.search(urlsplit(absolute).path):
            clean = absolute.split("#")[0]
            if clean not in careers:
                careers.append(clean)
    return boards, careers[:3]


def confirm_board(client: HttpClient, kind: ATSKind, board: str) -> bool:
    snap = ADAPTERS[kind].scan(client, board)
    return snap.complete


def resolve_company(
    client: HttpClient, name: str, website: str | None
) -> tuple[ATSKind, str, str] | None:
    if website:
        try:
            res = client.fetch(website, SITE_POLICY)
            boards, careers = find_ats_links(res.text, res.url)
            for career_url in careers[:2]:
                if boards:
                    break
                try:
                    page = client.fetch(career_url, SITE_POLICY)
                    boards, _ = find_ats_links(page.text, page.url)
                except (FetchError, BlockedURL) as e:
                    log.debug("career page %s: %s", career_url, e)
            for kind, board in boards:
                if confirm_board(client, kind, board):
                    return kind, board, "site-link"
        except (FetchError, BlockedURL) as e:
            log.debug("site %s: %s", website, e)

    gh: GreenhouseAdapter = ADAPTERS[ATSKind.GREENHOUSE]  # type: ignore[assignment]
    target = _norm_name(name)
    candidates = []
    for cand in (
        slugify(name).replace("-", ""),
        slugify(name),
        (canonical_domain(website) or "").split(".")[0],
    ):
        if cand and cand not in candidates:
            candidates.append(cand)
    for cand in candidates:
        try:
            board_name = gh.board_name(client, cand)
        except (FetchError, BlockedURL):
            continue
        if board_name and _norm_name(board_name) == target and target:
            return ATSKind.GREENHOUSE, cand, "greenhouse-name-match"
    return None
