# jobmonitor

Free-first monitor that discovers startups (YC → Techstars → a16z) and collects newly posted or
newly detected jobs open to people working **inside the USA** (onsite, hybrid, or remote that
explicitly accepts US applicants). Output goes to SQLite + CSV locally, with optional Google Sheets.
No runtime LLM, no paid services.

## Quick start (no credentials)

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements-dev.lock # Linux/py3.12 lock; elsewhere: pip install -e ".[dev,sheets]"
pip install --no-deps -e .

jobmonitor demo --out demo-output     # offline: synthetic boards, 2 simulated days -> demo-output/exports/*.csv
cp config.example.yaml config.yaml    # then edit
jobmonitor run --force-publish        # live: discover, collect, publish CSV to exports/
jobmonitor status
pytest                                # offline suite; `pytest -m network` = opt-in live smoke tests
```

Commands (`jobmonitor --help`): `discover`, `collect`, `publish`, `run`, `status`, `add-company`,
`reclassify`, `state restore|save`, `demo`, `unlock`, `resync-sheets`.
Exit codes: 0 ok/partial, 2 failed, 3 another run holds the lock, 4 credential error, 5 state conflict.

Curate boards manually: `jobmonitor add-company --name Acme --website https://acme.com --ats greenhouse --board acme`.

## How it works

```
discover (weekly)  YC (yc-oss dataset) / Techstars / a16z ─► companies (dedup by canonical domain,
                   provenance in company_sources, status evidence) ─► board resolution
collect (daily)    boards ─► Greenhouse / Lever / Ashby public APIs ─► snapshot ─► freshness engine
publish (every 3d) jobs ─► views (Companies, Jobs, NewJobs, LocationUnclear, Runs) ─► CSV / Sheets
```

| Module | Role |
|---|---|
| `db.py` | SQLite schema: companies, company_sources, boards (per-source checkpoints), source_jobs, jobs, runs, deliveries, publish_state, locks |
| `adapters/` | ATS clients + pure parsers. Verified endpoints: `boards-api.greenhouse.io/v1/boards/{b}/jobs`, `api.lever.co/v0/postings/{b}?mode=json&skip&limit`, `api.ashbyhq.com/posting-api/job-board/{b}` |
| `discovery/` | Source adapters (same interface) and `resolve.py` (board identifiers) |
| `freshness.py` | Baseline, new/repost labels, checkpoints, removal inference |
| `normalize.py` | Rule-based work mode and US eligibility |
| `netguard.py`, `http_client.py` | SSRF defense, redirects, limits, retries, rate limiting |
| `exports/` | Sanitized views, atomic CSV, Sheets keyed upserts |
| `statesync.py` | SQLite ↔ git state branch for ephemeral CI runners |

**Board resolution.** Company names are never assumed to be board ids. A board is accepted when
(1) the company's own site links to or embeds it (robots.txt respected) and the API confirms it
exists (`site-link`), or (2) a Greenhouse slug guess whose board metadata name equals the company
name (`greenhouse-name-match`). Lever and Ashby have no board-level name to check, so they are
never guessed. The evidence is stored in `boards.evidence`.

**Freshness rules.**
- A board's first complete scan is a *baseline*: its undated jobs are not new. Explicitly dated
  jobs published within `baseline_recent_days` are new (`source_published`).
- Later scans label unseen jobs `source_published` when the ATS publication time falls within the
  checkpoint minus `checkpoint_overlap_hours`. Otherwise they are `newly_detected`.
- `updated_at` is stored separately and never treated as creation. `source_published_at` keeps the
  earliest value seen, so a republish does not look new.
- Only complete snapshots (including HTTP 304) advance the checkpoint, update `last_seen_at`, or
  count toward closing. Partial or failed scans are discarded atomically.
- A job closes after `close_after_missing_scans` consecutive complete snapshots without it. If it
  reappears it reopens and is not counted as new. A closed job reappearing under a new id with the
  same title and location is labelled `repost_of` and is not new by default.
- Identity is (ATS, board, source job id). Requisitions with the same title stay separate rows.
- Coverage starts from each board's last *successful* scan, so weekends and missed days leave no
  gap. NewJobs covers the time since the last *confirmed* publication.

**Eligibility.** Any US location → `us`, whatever the HQ. A remote region that includes the US
(Americas, North America) → `remote_us_ok`. Plain "Remote", worldwide/global remote, or
unrecognized text → `location_unclear` (the LocationUnclear tab). Only non-US locations → `non_us`
(excluded). Structured ATS countries (Lever `country`, Ashby `addressCountry`) take precedence.
Seniority, citizenship and sponsorship are extracted only from explicit evidence, default to
`unknown`, and the filters for them are off by default; unknown values are never filtered out.
After a rules change, run `jobmonitor reclassify` to update stored jobs without any network calls.

## Security

- Every URL and every redirect hop is validated: https only (per policy), no credentials in URLs,
  ports 80/443 only, no localhost, `.local` or `.internal` names, no numeric or obfuscated hosts,
  plus per-source host allowlists. Redirect chains are capped.
- **DNS rebinding:** `GuardedBackend` resolves the name itself at TCP-connect time, rejects the
  host if *any* record is private, loopback, link-local, reserved, metadata, CGNAT or IPv4-mapped,
  and dials that exact IP. TLS SNI and certificate checks still use the hostname. Env proxies are
  ignored. If the guard cannot be installed, startup fails instead of running unprotected.
- Responses have byte limits and request deadlines, and TLS is always verified. HTML is parsed
  only as data, and embedded JSON is decoded with `json`. Nothing scraped is ever executed or
  passed to a shell. SQL is parameterized.
- CSV and Sheets cells are sanitized against formula injection (`= + - @`, tabs, full-width
  variants) and written to Sheets as `RAW`. Links pass through only if they are plain http(s).
- Logs go through a redaction filter (private keys, bearer tokens, GitHub/Google tokens, URL
  userinfo, `key=` parameters). Credential errors never include the key material.
- No Playwright fallback is shipped: no verified source currently needs JavaScript, and adding a
  browser would require the same network guard at the browser level first.

## Google Sheets (optional)

1. In Google Cloud: create a project, enable the **Google Sheets API**, and create a **service
   account** with no IAM roles. Create a JSON key and store it **outside** the repo.
2. Create a spreadsheet and share it with the service account email as **Editor**. Do not share
   anything else with it. The client requests only the `spreadsheets` scope.
3. Locally, set in `.env` (see `.env.example`): `JOBMONITOR_SHEETS_ENABLED=true`,
   `JOBMONITOR_SHEETS_SPREADSHEET_ID=<id from the sheet URL>`,
   `JOBMONITOR_GOOGLE_SERVICE_ACCOUNT_JSON=<path to key file>`.
4. Run `jobmonitor publish --force --sheets`. The tabs Companies, Jobs, NewJobs, LocationUnclear
   and Runs are created as needed.

Rows are upserted by the stable id in column A, and only changed rows are sent. Delivery state
lives in SQLite: rows are marked pending before the write and done only after the API confirms.
If a run crashes after a successful write, the next run reads the keys back and updates those rows
in place rather than appending duplicates. If rows were deleted by hand, run `jobmonitor resync-sheets`.

## GitHub Actions (`.github/workflows/`)

| Workflow | Trigger | Does |
|---|---|---|
| `scheduled.yml` | daily `7 9 * * *` and weekly `37 8 * * 1`, `timezone: America/Los_Angeles` | `run` (discover if due, collect, publish if due); Monday forces discovery |
| `manual.yml` | `workflow_dispatch` | any whitelisted command |
| `recovery.yml` | `workflow_dispatch` | unlock, bounded `collect --limit N`, `publish --force` (20 min cap) |
| `ci.yml` | push / PR | ruff + pytest on 3.12 and 3.13, `contents: read` |
| `_job.yml` | reusable | restore state → command → save state (always) → upload exports artifact |

Hardening: actions pinned to commit SHAs, least-privilege `permissions`, `timeout-minutes`, a
shared `jobmonitor-state` concurrency group with `cancel-in-progress: false`, a hash-locked
install, pip caching, secrets passed only through `env` (never interpolated into shell), and
command inputs restricted by a `case` whitelist.

Timezones: GitHub's documentation says schedules accept an IANA `timezone` and that runs falling
in a DST spring-forward gap move to the next valid time. 09:07 and 08:37 are never inside the
01:00–03:00 transition window, so DST doesn't skip or repeat them. Schedules are also set off the
hour because GitHub notes that scheduled runs can be delayed or dropped under load. **Delivery
times are best effort, not exact.**

**Durable state.** Runners are ephemeral, so `state restore` / `state save` keep the SQLite DB on
a branch named `state` (`jobmonitor.sqlite3.gz` + `manifest.json` with sha256 and schema
version). Each save is a single commit built with git plumbing; the working tree is untouched.
It is pushed with `--force-with-lease=refs/heads/state:<restored commit>` (compare-and-swap). If
another run moved the branch, the push is rejected with exit 5 and nothing is overwritten.
Restore verifies the checksum and refuses newer schemas. Save runs even after a failed command
step, so partial progress such as checkpoints of boards that succeeded is kept. The exports
artifact (7 days) is only a convenience copy; the state branch is the store of record. The DB holds
public job data only, never secrets.

### Setup steps

1. Create a **private** GitHub repo whose root is this `proj/` directory, then push `main`.
2. Commit a `config.yaml` (from `config.example.yaml`; it contains no secrets) and set the repository
   variable `JOBMONITOR_CONFIG_PATH=config.yaml`. Without it, `config.example.yaml` is used.
3. Optional Sheets: add secrets `JOBMONITOR_SHEETS_SPREADSHEET_ID` and
   `JOBMONITOR_GOOGLE_SERVICE_ACCOUNT_JSON` (paste the key JSON), and the variable
   `JOBMONITOR_SHEETS_ENABLED=true`.
4. Settings → Actions → General: keep the default workflow token permission at read; the
   workflows request `contents: write` themselves, since the state branch needs it. Add a
   **ruleset on `main`** (block force pushes, require PRs) so that token cannot change `main`.
5. Run **manual → run** once. It creates the `state` branch. Check the Runs tab or the run log.

## Runtime measurements (local, 2026-11-01, Windows, home connection)

| Step | Scope | Duration | Requests |
|---|---|---|---|
| discover | YC + Techstars + a16z, capped at 15 each (45), 33 board probes → 11 boards | 36.2 s | 147 |
| collect (first) | 13 boards, 1,077 jobs (880 US, 189 non-US, 8 unclear after `reclassify`) | 7.2 s | 13 |
| collect (repeat) | same boards, all `304 Not Modified` | 7.2 s | 13 |

Collection time is dominated by the 1 s per-host politeness interval. Since all boards share the
three ATS API hosts, 500 boards take roughly 500 / 3 / (1 req/s) ≈ 3 min, plus Lever pagination.
**Free-tier assumption:** a daily run of about 5 min plus weekly discovery is roughly 150–250
Actions minutes a month. Check your plan's current included private-repo minutes and storage. In
public repos, scheduled workflows are disabled after 60 days without repository activity.
Measure again on the runner before raising limits.

## Limitations

- **Techstars:** only the ~30 featured companies that are server-rendered. The full directory is
  queried through a client-side search key, and using that would mean scraping a third-party
  credential, so it is not used.
- **YC:** read from the open `yc-oss/api` mirror of the public directory (refreshed daily, no keys).
  Default scope is "currently hiring"; set `discovery.yc_batches` for specific batches.
- **Boards:** only Greenhouse, Lever (US instance) and Ashby. EU Lever, Workday and custom career
  pages are not collected. Companies whose sites render careers links only with JavaScript stay
  unresolved unless curated with `add-company`.
- Classification is rule-based. Unrecognized strings go to LocationUnclear, never into Jobs.
  "Remote | Canada"-style lists stay unclear on purpose.
- Repost detection requires an exact title and location match within 30 days.
- Sheets is tested against an in-memory fake. Not yet verified against the live Google API.
- GitHub workflows are validated by tests (structure, SHA pins, timezone), not yet executed on
  GitHub. Email notification is not implemented (disabled by design).
