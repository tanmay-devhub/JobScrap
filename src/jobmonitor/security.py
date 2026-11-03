"""Output sanitization and log redaction."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

# Leading characters that make spreadsheet apps interpret a cell as a formula / DDE.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_CELL_CHARS = 5000  # Sheets cell limit is 50k; keep exports small


def neutralize_cell(value) -> str:
    """Make untrusted text inert in CSV/Sheets. Numbers/None pass through as strings."""
    if value is None:
        return ""
    if isinstance(value, bool | int | float):
        return str(value)
    text = _CONTROL_RE.sub("", str(value))[:MAX_CELL_CHARS]
    stripped = text.lstrip(" ")
    if stripped.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def safe_link(url: str | None) -> str:
    """Return url only if it is a plain absolute http(s) link; otherwise empty."""
    if not url or not isinstance(url, str) or len(url) > 2048:
        return ""
    url = url.strip()
    if any(c in url for c in ("\r", "\n", "\t", " ", '"', "<", ">")):
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        return ""
    return url


def html_to_text(html: str | None, limit: int = 4000) -> str:
    """Strip markup from untrusted HTML; never rendered, never executed."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "object", "embed"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return text[:limit]


# ---------- redaction ----------
_SECRET_PATTERNS = [
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r'("private_key(?:_id)?"\s*:\s*")[^"]+(")'), r"\1[REDACTED]\2"),
    # Bearer before the generic key=value rule, which would otherwise consume only "Bearer".
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._\-~+/]+=*"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)\b(authorization|x-api-key|api[_-]?key|token|secret|password|passwd)\b(\s*[:=]\s*)(\"?)[^\s\"&,;]+"
        ),
        r"\1\2\3[REDACTED]",
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._\-]+"), "[REDACTED_GOOGLE_TOKEN]"),
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)([?&](?:key|token|access_token|sig|signature)=)[^&\s]+"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging crash the app
            return True
        record.msg = redact(msg)
        record.args = None
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
