from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, Field

from .enums import ATSKind, CompanyStatus, USEligibility, WorkMode


def job_identity(ats: ATSKind | str, board: str, source_job_id: str) -> str:
    """Stable identity per requisition: never derived from title, so distinct reqs stay distinct."""
    raw = f"{ats}:{board.lower()}:{source_job_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class DiscoveredCompany(BaseModel):
    name: str
    domain: str | None = None  # canonical domain, e.g. "example.com"
    website: str | None = None
    careers_url: str | None = None
    ats: ATSKind | None = None
    board: str | None = None  # resolved ATS board identifier
    status: CompanyStatus = CompanyStatus.UNKNOWN
    status_evidence: str | None = None
    source: str  # "yc" | "techstars" | "a16z" | "seed"
    source_ref: str | None = None  # source-specific id/slug/url
    batch: str | None = None


class RawJob(BaseModel):
    """What an ATS adapter returns, before eligibility classification."""

    source_job_id: str
    title: str
    job_url: str | None = None
    apply_url: str | None = None
    location: str = ""
    extra_locations: list[str] = Field(default_factory=list)
    workplace_type: str | None = None  # ATS-provided hint, e.g. "Remote", "Hybrid", "OnSite"
    country_codes: list[str] = Field(default_factory=list)  # ATS-structured countries if given
    department: str | None = None
    team: str | None = None
    employment_type: str | None = None
    published_at: datetime | None = None  # only when the ATS documents it as original publication
    updated_at: datetime | None = None
    description_text: str | None = None  # untrusted; plain text only, truncated


class Classification(BaseModel):
    work_mode: WorkMode
    eligibility: USEligibility
    reason: str
