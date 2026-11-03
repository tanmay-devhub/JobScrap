"""Durable SQLite state on a git branch (for ephemeral CI runners).

Layout of the state branch (single commit, no history by default):
    jobmonitor.sqlite3.gz   deterministic gzip of a VACUUM INTO snapshot
    manifest.json           {sha256, bytes, schema_version, saved_at, run}

Concurrency: `restore` records the branch commit it restored from. `save` pushes with
`--force-with-lease=refs/heads/<branch>:<that commit>`, i.e. compare-and-swap: if another run
updated the branch in between, the push is rejected and nothing is overwritten.
All git calls use argument lists (no shell); no scraped data reaches a command line.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .db import SCHEMA_VERSION, Store
from .timeutil import iso, utcnow

log = logging.getLogger(__name__)
DB_NAME = "jobmonitor.sqlite3.gz"
MANIFEST = "manifest.json"
MAX_STATE_BYTES = 90 * 1024 * 1024  # GitHub rejects files > 100 MB


class StateError(Exception):
    pass


class StateConflict(StateError):
    """The state branch moved since restore; refusing to overwrite."""


@dataclass
class GitState:
    repo: Path
    branch: str = "state"
    remote: str = "origin"

    def _git(
        self, *args: str, input_bytes: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "jobmonitor-bot",
            "GIT_AUTHOR_EMAIL": "jobmonitor-bot@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "jobmonitor-bot",
            "GIT_COMMITTER_EMAIL": "jobmonitor-bot@users.noreply.github.com",
        }
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            input=input_bytes,
            capture_output=True,
            env=env,
            check=False,
        )
        if check and proc.returncode != 0:
            raise StateError(
                f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()[:400]}"
            )
        return proc

    @property
    def base_file(self) -> Path:
        return self.repo / ".git" / "jobmonitor-state-base"

    def remote_head(self) -> str | None:
        proc = self._git("ls-remote", "--heads", self.remote, f"refs/heads/{self.branch}")
        line = proc.stdout.decode().strip()
        return line.split()[0] if line else None

    def restore(self, db_path: str | Path) -> bool:
        """Fetch the state branch and write the DB. Returns False when no state exists yet."""
        head = self.remote_head()
        if head is None:
            self.base_file.write_text("", encoding="utf-8")  # lease: branch must not exist
            log.info("no state branch %r yet; starting fresh", self.branch)
            return False
        self._git("fetch", "--no-tags", "--depth=1", self.remote, f"refs/heads/{self.branch}")
        manifest = json.loads(self._git("show", f"{head}:{MANIFEST}").stdout)
        blob = self._git("show", f"{head}:{DB_NAME}").stdout
        raw = gzip.decompress(blob)
        if hashlib.sha256(raw).hexdigest() != manifest.get("sha256"):
            raise StateError("state checksum mismatch; refusing to restore corrupted DB")
        if int(manifest.get("schema_version", 0)) > SCHEMA_VERSION:
            raise StateError("state was written by a newer schema version")
        dest = Path(db_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".restore-tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, dest)
        self.base_file.write_text(head, encoding="utf-8")
        log.info(
            "restored state %s (%d bytes, saved %s)", head[:10], len(raw), manifest.get("saved_at")
        )
        return True

    def save(self, store: Store, run: str = "") -> str:
        if not self.base_file.exists():
            raise StateError("restore was not run in this checkout; refusing blind overwrite")
        base = self.base_file.read_text(encoding="utf-8").strip()
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snap.sqlite3"
            store.snapshot_to(snap)
            # Locks are runner-local; CI concurrency groups serialize runs across runners.
            snap_store = Store(snap)
            snap_store.break_locks()
            snap_store.close()
            raw = snap.read_bytes()
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=9) as gz:
            gz.write(raw)
        blob = buf.getvalue()
        if len(blob) > MAX_STATE_BYTES:
            raise StateError(f"state too large ({len(blob)} bytes); lower retention_days")
        manifest = json.dumps(
            {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "schema_version": SCHEMA_VERSION,
                "saved_at": iso(utcnow()),
                "run": run,
            },
            indent=2,
            sort_keys=True,
        ).encode()
        db_sha = self._git("hash-object", "-w", "--stdin", input_bytes=blob).stdout.decode().strip()
        man_sha = (
            self._git("hash-object", "-w", "--stdin", input_bytes=manifest).stdout.decode().strip()
        )
        tree_spec = f"100644 blob {db_sha}\t{DB_NAME}\n100644 blob {man_sha}\t{MANIFEST}\n".encode()
        tree = self._git("mktree", input_bytes=tree_spec).stdout.decode().strip()
        commit = (
            self._git("commit-tree", tree, "-m", f"state: {run or 'update'}")
            .stdout.decode()
            .strip()
        )
        push = self._git(
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/{self.branch}:{base}",
            self.remote,
            f"{commit}:refs/heads/{self.branch}",
            check=False,
        )
        if push.returncode != 0:
            err = (push.stderr + push.stdout).decode(errors="replace")
            if "stale info" in err or "rejected" in err or "fetch first" in err:
                raise StateConflict("state branch changed since restore; not overwriting")
            raise StateError(f"git push failed: {err.strip()[:400]}")
        self.base_file.write_text(commit, encoding="utf-8")
        log.info("saved state %s (%d bytes compressed)", commit[:10], len(blob))
        return commit
