"""Google Sheets publisher: keyed upserts, RAW values, delivery state for safe retries.

Protocol per tab:
1. Compute desired rows; row key = column A. Hash each row.
2. Skip rows whose last *confirmed* delivery has the same hash.
3. Mark the rest `pending` in SQLite (committed before any network call).
4. Re-read the tab's key column, so rows written by an attempt that crashed before
   confirmation are found and updated in place rather than appended again.
5. Write updates + new rows with values.batchUpdate (RAW). Only after the API confirms,
   mark those deliveries `done`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

from ..db import Store
from ..timeutil import iso, utcnow
from .views import View

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CHUNK_ROWS = 500


class CredentialError(Exception):
    """Missing/invalid credentials or the service account lacks access to the spreadsheet."""


class SheetsError(Exception):
    """Sheets API failure that is not a credential problem."""


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _quote(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


class SheetsBackend(ABC):
    @abstractmethod
    def tab_names(self) -> list[str]: ...

    @abstractmethod
    def add_tabs(self, names: list[str]) -> None: ...

    @abstractmethod
    def read_column_a(self, tab: str) -> list[str]: ...

    @abstractmethod
    def batch_write(self, data: list[tuple[str, list[list[str]]]]) -> None:
        """data: [(A1 range, rows)] written with valueInputOption=RAW."""


class GoogleSheetsBackend(SheetsBackend):
    def __init__(self, spreadsheet_id: str, service_account: str):
        try:
            from google.oauth2 import service_account as sa
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
        except ImportError as e:  # pragma: no cover
            raise CredentialError("Sheets support not installed: pip install '.[sheets]'") from e
        self._HttpError = HttpError
        info = _load_service_account(service_account)
        try:
            creds = sa.Credentials.from_service_account_info(info, scopes=SCOPES)
        except (ValueError, KeyError):
            raise CredentialError("service account JSON is invalid") from None
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
        self._id = spreadsheet_id

    def _exec(self, req):
        from google.auth.exceptions import GoogleAuthError

        try:
            return req.execute(num_retries=5)  # client retries 429/5xx with backoff
        except GoogleAuthError as e:
            raise CredentialError(f"google auth failed: {type(e).__name__}") from None
        except self._HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in (401, 403):
                raise CredentialError(
                    f"sheets access denied (HTTP {status}); share the spreadsheet with the service account"
                ) from None
            if status == 404:
                raise CredentialError(
                    "spreadsheet not found; check JOBMONITOR_SHEETS_SPREADSHEET_ID"
                ) from None
            raise SheetsError(f"sheets API HTTP {status}") from None

    def tab_names(self):
        meta = self._exec(self._svc.get(spreadsheetId=self._id, fields="sheets.properties.title"))
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def add_tabs(self, names):
        reqs = [{"addSheet": {"properties": {"title": n}}} for n in names]
        self._exec(self._svc.batchUpdate(spreadsheetId=self._id, body={"requests": reqs}))

    def read_column_a(self, tab):
        res = self._exec(
            self._svc.values().get(
                spreadsheetId=self._id,
                range=f"{_quote(tab)}!A:A",
                valueRenderOption="UNFORMATTED_VALUE",
            )
        )
        return [row[0] if row else "" for row in res.get("values", [])]

    def batch_write(self, data):
        body = {
            "valueInputOption": "RAW",
            "data": [{"range": rng, "values": rows} for rng, rows in data],
        }
        self._exec(self._svc.values().batchUpdate(spreadsheetId=self._id, body=body))


def _load_service_account(value: str) -> dict:
    """Accepts a path to the JSON file or the JSON text itself. Never logs contents."""
    text = value.strip()
    if not text.startswith("{"):
        p = Path(os.path.expanduser(text))
        if not p.is_file():
            raise CredentialError("service account file not found")
        text = p.read_text(encoding="utf-8")
    try:
        info = json.loads(text)
    except json.JSONDecodeError:
        raise CredentialError("service account JSON could not be parsed") from None
    if info.get("type") != "service_account" or "private_key" not in info:
        raise CredentialError("credential is not a service-account key")
    return info


def _row_hash(header: list[str], row: list[str]) -> str:
    return hashlib.sha256(json.dumps([header, row]).encode()).hexdigest()[:32]


def publish_views(store: Store, backend: SheetsBackend, views: list[View]) -> dict[str, int]:
    """Returns rows written per tab. Raises CredentialError/SheetsError on failure."""
    existing_tabs = set(backend.tab_names())
    missing = [v.name for v in views if v.name not in existing_tabs]
    if missing:
        backend.add_tabs(missing)
    written: dict[str, int] = {}
    for view in views:
        written[view.name] = _publish_view(store, backend, view)
    return written


def _publish_view(store: Store, backend: SheetsBackend, view: View) -> int:
    target = f"sheets:{view.name}"
    hashes = {row[0]: _row_hash(view.header, row) for row in view.rows if row and row[0]}
    done = {
        r["row_key"]: r["payload_hash"]
        for r in store.q(
            "SELECT row_key, payload_hash FROM deliveries WHERE target=? AND status='done'",
            (target,),
        )
    }
    header_hash = _row_hash(view.header, [])
    header_done = done.get("__header__") == header_hash
    todo = [row for row in view.rows if row and row[0] and done.get(row[0]) != hashes[row[0]]]
    if not todo and header_done:
        return 0

    now = iso(utcnow())
    with store.tx() as c:
        for row in todo:
            c.execute(
                """INSERT INTO deliveries(target, row_key, payload_hash, status, attempts, updated_at)
                   VALUES (?,?,?,'pending',1,?) ON CONFLICT(target, row_key) DO UPDATE SET
                   payload_hash=excluded.payload_hash, status='pending',
                   attempts=deliveries.attempts+1, updated_at=excluded.updated_at""",
                (target, row[0], hashes[row[0]], now),
            )

    keys = backend.read_column_a(view.name)
    position = {k: i + 1 for i, k in enumerate(keys) if k and i > 0}  # 1-based, skip header
    next_row = max(len(keys) + 1, 2)
    last_col = col_letter(len(view.header))
    tab = _quote(view.name)
    data: list[tuple[str, list[list[str]]]] = [(f"{tab}!A1:{last_col}1", [view.header])]
    appends: list[list[str]] = []
    for row in todo:
        pos = position.get(row[0])
        if pos:
            data.append((f"{tab}!A{pos}:{last_col}{pos}", [row]))
        else:
            appends.append(row)
    for i in range(0, len(appends), CHUNK_ROWS):
        chunk = appends[i : i + CHUNK_ROWS]
        data.append((f"{tab}!A{next_row}:{last_col}{next_row + len(chunk) - 1}", chunk))
        next_row += len(chunk)

    try:
        for i in range(0, len(data), CHUNK_ROWS):
            backend.batch_write(data[i : i + CHUNK_ROWS])
    except Exception as e:
        with store.tx() as c:
            c.execute(
                "UPDATE deliveries SET last_error=? WHERE target=? AND status='pending'",
                (type(e).__name__, target),
            )
        raise

    with store.tx() as c:
        for row in todo:
            c.execute(
                "UPDATE deliveries SET status='done', last_error=NULL, updated_at=? "
                "WHERE target=? AND row_key=? AND payload_hash=?",
                (iso(utcnow()), target, row[0], hashes[row[0]]),
            )
        c.execute(
            """INSERT INTO deliveries(target, row_key, payload_hash, status, attempts, updated_at)
               VALUES (?, '__header__', ?, 'done', 1, ?) ON CONFLICT(target, row_key)
               DO UPDATE SET payload_hash=excluded.payload_hash, status='done'""",
            (target, header_hash, iso(utcnow())),
        )
    log.info("sheets %s: %d rows written (%d appended)", view.name, len(todo), len(appends))
    return len(todo)
