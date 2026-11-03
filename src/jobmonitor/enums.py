from enum import StrEnum


class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class USEligibility(StrEnum):
    US = "us"  # physically located in the US (onsite/hybrid/remote-in-US)
    REMOTE_US_OK = "remote_us_ok"  # global/multi-region remote with evidence US applicants accepted
    LOCATION_UNCLEAR = "location_unclear"
    NON_US = "non_us"  # excluded


class CompanyStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ACQUIRED = "acquired"
    PUBLIC = "public"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class FreshnessBasis(StrEnum):
    SOURCE_PUBLISHED = "source_published"  # ATS gives an explicit original publication time
    NEWLY_DETECTED = (
        "newly_detected"  # first seen on an already-baselined board, no source pub date
    )
    BASELINE = "baseline"  # present on first scan of a board; not claimed as new


class ATSKind(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"


class ScanOutcome(StrEnum):
    SUCCESS = "success"  # complete snapshot
    ZERO_JOBS = "zero_jobs"  # complete snapshot, genuinely no postings
    NOT_FOUND = "not_found"  # board identifier does not exist
    SOURCE_ERROR = "source_error"  # network/HTTP/parse failure; checkpoint not advanced
    PARTIAL = "partial"  # incomplete (e.g. pagination failed); checkpoint not advanced
    BLOCKED = "blocked"  # policy/robots/netguard refused
