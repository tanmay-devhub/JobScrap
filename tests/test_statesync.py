import shutil
import subprocess

import pytest

from jobmonitor.db import Store
from jobmonitor.models import DiscoveredCompany
from jobmonitor.statesync import GitState, StateConflict, StateError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "--bare", "-q", str(remote), cwd=tmp_path)

    def clone(name):
        path = tmp_path / name
        git("init", "-q", str(path), cwd=tmp_path)
        git("remote", "add", "origin", str(remote), cwd=path)
        return path

    return clone


def test_restore_save_roundtrip(repos, tmp_path):
    a = repos("a")
    gs = GitState(a)
    db = tmp_path / "a.sqlite3"
    assert gs.restore(db) is False  # no branch yet
    s = Store(db)
    s.upsert_company(DiscoveredCompany(name="Acme", domain="acme.example", source="t"))
    with s.lock("collect"):
        gs.save(s, "run-1")  # a held lock must not be persisted
    s.close()

    b = repos("b")
    db_b = tmp_path / "b.sqlite3"
    assert GitState(b).restore(db_b) is True
    s2 = Store(db_b)
    assert s2.one("SELECT name FROM companies")["name"] == "Acme"
    assert s2.q("SELECT * FROM locks") == []
    s2.close()


def test_concurrent_save_is_rejected(repos, tmp_path):
    a, b = repos("a"), repos("b")
    sa, sb = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    ga, gb = GitState(a), GitState(b)
    ga.restore(tmp_path / "a.db")
    gb.restore(tmp_path / "b.db")
    ga.save(sa, "a")
    with pytest.raises(StateConflict):
        gb.save(sb, "b")  # b restored before a saved -> stale lease
    # after re-restoring, b may save
    gb.restore(tmp_path / "b2.db")
    gb.save(Store(tmp_path / "b2.db"), "b-retry")


def test_save_without_restore_refused(repos, tmp_path):
    with pytest.raises(StateError):
        GitState(repos("a")).save(Store(tmp_path / "x.db"))


def test_corrupted_state_refused(repos, tmp_path):
    a = repos("a")
    gs = GitState(a)
    gs.restore(tmp_path / "a.db")
    gs.save(Store(tmp_path / "a.db"), "a")

    def run(*args, data=None):
        return subprocess.run(
            ["git", "-C", str(a), *args], input=data, capture_output=True, check=True
        ).stdout

    # tamper: same DB blob, but a manifest whose checksum does not match
    db_entry = next(e for e in run("ls-tree", gs.remote_head()).splitlines() if b"sqlite3.gz" in e)
    bad = (
        run("hash-object", "-w", "--stdin", data=b'{"sha256": "0", "schema_version": 1}')
        .decode()
        .strip()
    )
    tree = (
        run("mktree", data=db_entry + f"\n100644 blob {bad}\tmanifest.json\n".encode())
        .decode()
        .strip()
    )
    commit = (
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "x")
        .decode()
        .strip()
    )
    run("push", "-q", "-f", "origin", f"{commit}:refs/heads/state")
    with pytest.raises(StateError, match="checksum"):
        GitState(repos("c")).restore(tmp_path / "c.db")
