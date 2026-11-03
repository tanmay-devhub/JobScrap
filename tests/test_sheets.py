import json

import pytest

from jobmonitor.exports.sheets import (
    CredentialError,
    SheetsBackend,
    _load_service_account,
    col_letter,
    publish_views,
)
from jobmonitor.exports.views import View


class FakeSheets(SheetsBackend):
    """In-memory spreadsheet honoring A1 ranges written by batch_write."""

    def __init__(self):
        self.tabs: dict[str, dict[int, list[str]]] = {}
        self.fail_next_write = False
        self.writes = 0

    def tab_names(self):
        return list(self.tabs)

    def add_tabs(self, names):
        for n in names:
            self.tabs.setdefault(n, {})

    def read_column_a(self, tab):
        rows = self.tabs[tab]
        if not rows:
            return []
        return [rows.get(i, [""])[0] for i in range(1, max(rows) + 1)]

    def batch_write(self, data):
        if self.fail_next_write:
            self.fail_next_write = False
            raise RuntimeError("network down")
        self.writes += 1
        for rng, values in data:
            tab, cells = rng.rsplit("!", 1)
            tab = tab.strip("'").replace("''", "'")
            start = int("".join(ch for ch in cells.split(":")[0] if ch.isdigit()))
            for i, row in enumerate(values):
                self.tabs[tab][start + i] = row

    def data_rows(self, tab):
        rows = self.tabs[tab]
        return [rows[i] for i in sorted(rows) if i > 1]


def view(rows):
    return View("Jobs", ["job_id", "title"], [list(r) for r in rows])


def test_upsert_and_no_duplicates_on_rerun(store):
    fake = FakeSheets()
    publish_views(store, fake, [view([("a", "A"), ("b", "B")])])
    assert fake.data_rows("Jobs") == [["a", "A"], ["b", "B"]]
    writes = fake.writes
    publish_views(store, fake, [view([("a", "A"), ("b", "B")])])
    assert fake.writes == writes  # nothing changed -> no API write
    publish_views(store, fake, [view([("a", "A2"), ("b", "B"), ("c", "C")])])
    assert fake.data_rows("Jobs") == [["a", "A2"], ["b", "B"], ["c", "C"]]


def test_failed_write_stays_pending_and_retry_does_not_duplicate(store):
    fake = FakeSheets()
    publish_views(store, fake, [view([("a", "A")])])
    fake.fail_next_write = True
    with pytest.raises(RuntimeError):
        publish_views(store, fake, [view([("a", "A"), ("b", "B")])])
    pending = store.q(
        "SELECT row_key, status FROM deliveries WHERE target='sheets:Jobs' AND row_key='b'"
    )
    assert pending[0]["status"] == "pending"
    publish_views(store, fake, [view([("a", "A"), ("b", "B")])])
    assert fake.data_rows("Jobs") == [["a", "A"], ["b", "B"]]


def test_crash_after_write_before_confirmation_does_not_duplicate(store, monkeypatch):
    """API succeeded but process died before marking done: re-read keys => update in place."""
    fake = FakeSheets()
    import jobmonitor.exports.sheets as sh

    real_tx = store.tx
    state = {"n": 0}

    def crashing_tx():
        state["n"] += 1
        if state["n"] == 2:  # 1st tx marks pending, 2nd would mark done
            raise SystemExit("killed")
        return real_tx()

    monkeypatch.setattr(store, "tx", crashing_tx)
    with pytest.raises(SystemExit):
        sh.publish_views(store, fake, [view([("a", "A")])])
    monkeypatch.setattr(store, "tx", real_tx)
    sh.publish_views(store, fake, [view([("a", "A")])])
    assert fake.data_rows("Jobs") == [["a", "A"]]


def test_col_letter():
    assert [col_letter(n) for n in (1, 26, 27, 52, 703)] == ["A", "Z", "AA", "AZ", "AAA"]


def test_service_account_validation(tmp_path):
    with pytest.raises(CredentialError):
        _load_service_account(str(tmp_path / "missing.json"))
    with pytest.raises(CredentialError):
        _load_service_account('{"type": "authorized_user"}')
    with pytest.raises(CredentialError):
        _load_service_account("{not json")
    good = {"type": "service_account", "private_key": "x", "client_email": "a@b"}
    p = tmp_path / "sa.json"
    p.write_text(json.dumps(good))
    assert _load_service_account(str(p))["client_email"] == "a@b"


def test_credential_error_message_never_contains_key(tmp_path):
    secret = '{"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----SECRETKEY"}'
    try:
        from jobmonitor.exports.sheets import GoogleSheetsBackend

        GoogleSheetsBackend("sheet-id", secret)
    except CredentialError as e:
        assert "SECRETKEY" not in str(e)
    except ImportError:
        pytest.skip("sheets extra not installed")
