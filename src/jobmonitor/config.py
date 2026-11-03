"""Configuration: YAML file for behaviour, environment variables for secrets/paths."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel as _PydanticBase
from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseModel(_PydanticBase):
    model_config = ConfigDict(extra="forbid")  # a typo in config.yaml is an error, not a no-op


class HttpConfig(BaseModel):
    user_agent: str = "jobmonitor/0.1 (+https://github.com/; contact via repo issues)"
    timeout_seconds: float = 20.0
    request_deadline_seconds: float = 45.0
    max_response_bytes: int = 15 * 1024 * 1024
    max_redirects: int = 5
    max_concurrency: int = 4
    per_host_concurrency: int = 2
    per_host_min_interval_seconds: float = 1.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    max_retry_after_seconds: float = 120.0


class FreshnessConfig(BaseModel):
    # explicitly dated jobs on a brand-new board count as new if published within this window
    baseline_recent_days: int = 3
    # overlap applied to the checkpoint when comparing source publication times
    checkpoint_overlap_hours: int = 6
    # consecutive complete snapshots a job must be absent from before it is closed
    close_after_missing_scans: int = 2
    # closed job with identical company/title/location reappearing under a new id = repost
    repost_window_days: int = 30
    reposts_count_as_new: bool = False
    disable_board_after_not_found: int = 5
    retention_days: int = 180


class FilterConfig(BaseModel):
    """Optional, off by default. Unknown values are never filtered out."""

    # "Remote - Worldwide" without explicit US evidence goes to LocationUnclear unless enabled.
    worldwide_remote_counts_as_us: bool = False
    title_include_regex: str | None = None
    title_exclude_regex: str | None = None
    departments_exclude: list[str] = Field(default_factory=list)
    seniority_exclude: list[str] = Field(default_factory=list)  # e.g. ["intern", "executive"]
    exclude_citizenship_required: bool = False  # only excludes jobs with explicit evidence
    exclude_no_sponsorship: bool = False  # only excludes jobs with explicit evidence


class DiscoveryConfig(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["yc", "techstars", "a16z"])
    yc_batches: list[str] = Field(default_factory=list)  # empty = all available
    max_companies_per_source: int = 200
    probe_ats: bool = True  # probe ATS APIs to resolve board identifiers
    max_probes_per_run: int = 300


class CollectConfig(BaseModel):
    max_boards_per_run: int = 500
    fetch_descriptions: bool = False


class PublishConfig(BaseModel):
    every_days: int = 3  # publication cadence; NewJobs covers since the last successful publish
    csv_dir: str = "exports"
    sheets_enabled: bool = False


class AppConfig(BaseModel):
    http: HttpConfig = Field(default_factory=HttpConfig)
    freshness: FreshnessConfig = Field(default_factory=FreshnessConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    collect: CollectConfig = Field(default_factory=CollectConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    # Manually curated companies (name, domain, ats, board) merged with discovery results.
    seed_companies: list[dict] = Field(default_factory=list)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOBMONITOR_", env_file=".env", extra="ignore")

    config_path: str = "config.yaml"
    db_path: str = "data/jobmonitor.sqlite3"
    log_level: str = "INFO"
    sheets_enabled: bool | None = None  # overrides publish.sheets_enabled from the YAML file
    sheets_spreadsheet_id: str | None = None
    # Path to the service-account JSON file, or the JSON itself (CI secret). Never logged.
    google_service_account_json: str | None = None


def load_config(path: str | Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    p = Path(path)
    if not p.exists():
        return AppConfig()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
