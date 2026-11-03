"""Location -> work mode + US eligibility. Deterministic, rule-based, conservative.

Each location fragment is classified independently, then combined:
  any US fragment                      -> US
  remote with US-inclusive region      -> REMOTE_US_OK  (e.g. "Remote - Americas")
  anything ambiguous                   -> LOCATION_UNCLEAR
  only non-US fragments                -> NON_US (excluded)
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from .enums import USEligibility, WorkMode
from .models import Classification, RawJob

US_STATES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
    "puerto rico": "PR",
}
US_STATE_CODES = set(US_STATES.values())

US_CITIES = {
    "san francisco",
    "sf",
    "bay area",
    "sf bay area",
    "silicon valley",
    "palo alto",
    "mountain view",
    "menlo park",
    "san jose",
    "sunnyvale",
    "redwood city",
    "san mateo",
    "oakland",
    "berkeley",
    "los angeles",
    "santa monica",
    "san diego",
    "irvine",
    "new york",
    "new york city",
    "nyc",
    "brooklyn",
    "manhattan",
    "seattle",
    "bellevue",
    "redmond",
    "boston",
    "cambridge, ma",
    "austin",
    "dallas",
    "houston",
    "chicago",
    "denver",
    "boulder",
    "atlanta",
    "miami",
    "washington dc",
    "washington, d.c.",
    "dc",
    "philadelphia",
    "pittsburgh",
    "portland",
    "salt lake city",
    "phoenix",
    "minneapolis",
    "detroit",
    "nashville",
    "raleigh",
    "durham",
    "charlotte",
    "columbus",
    "baltimore",
    "st. louis",
    "kansas city",
    "las vegas",
    "honolulu",
    "anchorage",
    "arlington",
    "reston",
    "mclean",
}

NON_US_COUNTRIES = {
    "canada",
    "mexico",
    "brazil",
    "argentina",
    "chile",
    "colombia",
    "peru",
    "uruguay",
    "united kingdom",
    "uk",
    "england",
    "scotland",
    "wales",
    "ireland",
    "france",
    "germany",
    "spain",
    "portugal",
    "italy",
    "netherlands",
    "belgium",
    "switzerland",
    "austria",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "poland",
    "czech republic",
    "czechia",
    "romania",
    "hungary",
    "greece",
    "ukraine",
    "turkey",
    "israel",
    "uae",
    "united arab emirates",
    "saudi arabia",
    "egypt",
    "nigeria",
    "kenya",
    "south africa",
    "india",
    "pakistan",
    "bangladesh",
    "sri lanka",
    "singapore",
    "malaysia",
    "indonesia",
    "philippines",
    "vietnam",
    "thailand",
    "japan",
    "south korea",
    "korea",
    "china",
    "hong kong",
    "taiwan",
    "australia",
    "new zealand",
    "estonia",
    "lithuania",
    "latvia",
    "serbia",
    "croatia",
    "bulgaria",
    "luxembourg",
    "costa rica",
    "guatemala",
}
NON_US_CITIES = {
    "london",
    "manchester",
    "edinburgh",
    "dublin",
    "paris",
    "berlin",
    "munich",
    "hamburg",
    "amsterdam",
    "rotterdam",
    "brussels",
    "zurich",
    "geneva",
    "vienna",
    "stockholm",
    "oslo",
    "copenhagen",
    "helsinki",
    "warsaw",
    "krakow",
    "prague",
    "budapest",
    "bucharest",
    "lisbon",
    "madrid",
    "barcelona",
    "milan",
    "rome",
    "athens",
    "kyiv",
    "istanbul",
    "tel aviv",
    "dubai",
    "abu dhabi",
    "riyadh",
    "cairo",
    "lagos",
    "nairobi",
    "cape town",
    "johannesburg",
    "bangalore",
    "bengaluru",
    "mumbai",
    "delhi",
    "new delhi",
    "gurgaon",
    "gurugram",
    "noida",
    "hyderabad",
    "chennai",
    "pune",
    "karachi",
    "lahore",
    "dhaka",
    "singapore",
    "kuala lumpur",
    "jakarta",
    "manila",
    "ho chi minh city",
    "hanoi",
    "bangkok",
    "tokyo",
    "osaka",
    "seoul",
    "beijing",
    "shanghai",
    "shenzhen",
    "hong kong",
    "taipei",
    "sydney",
    "melbourne",
    "brisbane",
    "auckland",
    "toronto",
    "vancouver",
    "montreal",
    "ottawa",
    "calgary",
    "waterloo",
    "mexico city",
    "guadalajara",
    "sao paulo",
    "são paulo",
    "rio de janeiro",
    "buenos aires",
    "santiago",
    "bogota",
    "bogotá",
    "medellin",
    "lima",
    "montevideo",
    "tallinn",
    "vilnius",
    "riga",
    "belgrade",
    "zagreb",
    "sofia",
    "cracow",
}
CA_PROVINCE_CODES = {"ON", "BC", "QC", "AB", "MB", "NS", "NB", "NL", "SK", "PE"}
CA_PROVINCES = {
    "ontario",
    "quebec",
    "british columbia",
    "alberta",
    "manitoba",
    "nova scotia",
    "new brunswick",
    "saskatchewan",
    "newfoundland",
}
# ISO codes that cannot be confused with a US state abbreviation (so no CA, DE, IN, GA, CO, ...).
NON_US_CODES = {
    "UK",
    "GB",
    "GBR",
    "CAN",
    "CH",
    "FR",
    "FRA",
    "ES",
    "IT",
    "NL",
    "BE",
    "SE",
    "NO",
    "DK",
    "FI",
    "PL",
    "PT",
    "IE",
    "JP",
    "KR",
    "CN",
    "SG",
    "AU",
    "NZ",
    "BR",
    "MX",
    "ISR",
    "DEU",
    "IND",
    "AUS",
    "MEX",
    "BRA",
    "ESP",
    "ITA",
    "NLD",
    "SWE",
    "POL",
    "PRT",
    "IRL",
    "JPN",
    "SGP",
    "CHE",
}


def _fold(text: str) -> str:
    """Lower-case and strip accents: 'Zürich' -> 'zurich', 'São Paulo' -> 'sao paulo'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


NON_US_REGIONS = {
    "emea",
    "europe",
    "eu",
    "apac",
    "asia",
    "latam",
    "latin america",
    "south america",
    "africa",
    "middle east",
    "oceania",
    "anz",
    "dach",
    "nordics",
    "benelux",
    "cet",
    "cest",
}
US_INCLUSIVE_REGIONS = {"americas", "north america", "na", "amer", "us/canada", "us & canada"}
GLOBAL_WORDS = {"anywhere", "worldwide", "global", "globally", "international"}
US_WORDS = {"united states", "united states of america", "usa", "u.s.", "u.s.a.", "us", "u.s"}
US_TIMEZONES = {"est", "edt", "pst", "pdt", "cst", "cdt", "mst", "mdt", "et", "pt", "ct", "mt"}
REMOTE_RE = re.compile(
    r"\b(remote|anywhere|distributed|work from home|wfh|telecommute|virtual)\b", re.I
)
HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
ONSITE_RE = re.compile(r"\b(on[- ]?site|in[- ]office|in office)\b", re.I)
SPLIT_RE = re.compile(r"\s*(?:\||;|\n|/(?=\s)|\s+or\s+|\s+and\s+|•)\s*", re.I)
WORD_RE = re.compile(r"[a-z][a-z.\-'&/]*", re.I)
US_COUNTRY_CODES = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA", "PR"}


class Frag(StrEnum):
    US = "us"
    US_REGION = "us_region"  # region including US (Americas / North America)
    NON_US = "non_us"
    GLOBAL = "global"
    UNKNOWN = "unknown"


def _has_phrase(text: str, phrases: set[str]) -> str | None:
    for p in sorted(phrases, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", text):
            return p
    return None


def classify_fragment(fragment: str) -> tuple[Frag, str]:
    raw = fragment.strip()
    t = _fold(raw)
    if not t:
        return Frag.UNKNOWN, "empty"
    # Upper-case tokens (state / province codes) are checked on the original text.
    codes = set(re.findall(r"(?:,|\()\s*([A-Z]{2,3})\b", raw)) | set(
        re.findall(r"\b([A-Z]{2,3})\s*\)?\s*$", raw)
    )

    # An explicit US mention wins even alongside other countries ("US/Canada", "Canada or US").
    # Bare "us" only counts when written upper-case, so "join us" is not a location.
    if (m := _has_phrase(t, US_WORDS)) and (
        m != "us" or re.search(r"(?<![A-Za-z])US(?![A-Za-z])", raw)
    ):
        return Frag.US, f"us:{m}"
    # "New Mexico" is a US state; the country match must not be part of it.
    if (m := _has_phrase(t, NON_US_COUNTRIES)) and not (m == "mexico" and "new mexico" in t):
        return Frag.NON_US, f"country:{m}"
    if m := _has_phrase(t, NON_US_CITIES):
        return Frag.NON_US, f"city:{m}"
    if m := _has_phrase(t, CA_PROVINCES):
        return Frag.NON_US, f"province:{m}"
    if (codes & (CA_PROVINCE_CODES | NON_US_CODES)) and not codes & US_STATE_CODES:
        return Frag.NON_US, f"code:{sorted(codes & (CA_PROVINCE_CODES | NON_US_CODES))[0]}"
    if codes & US_STATE_CODES:
        return Frag.US, f"state:{sorted(codes & US_STATE_CODES)[0]}"
    if m := _has_phrase(t, set(US_STATES)):
        return Frag.US, f"state:{m}"
    if m := _has_phrase(t, US_CITIES):
        return Frag.US, f"city:{m}"
    if (m := _has_phrase(t, US_INCLUSIVE_REGIONS)) and (m != "na" or re.search(r"\bNA\b", raw)):
        return Frag.US_REGION, f"region:{m}"
    if m := _has_phrase(t, NON_US_REGIONS):
        return Frag.NON_US, f"region:{m}"
    if m := _has_phrase(t, US_TIMEZONES) if REMOTE_RE.search(t) else None:
        return Frag.US, f"us-timezone:{m}"
    if m := _has_phrase(t, GLOBAL_WORDS):
        return Frag.GLOBAL, f"global:{m}"
    return Frag.UNKNOWN, "unrecognized"


def split_locations(job: RawJob) -> list[str]:
    frags: list[str] = []
    for loc in [job.location, *job.extra_locations]:
        if not loc:
            continue
        for part in SPLIT_RE.split(loc):
            part = part.strip(" -–—,")
            if part and part not in frags:
                frags.append(part)
    return frags


def _work_mode(job: RawJob, frags: list[str]) -> WorkMode:
    hint = (job.workplace_type or "").lower().replace("-", "").replace(" ", "").replace("_", "")
    if hint in ("onsite", "inoffice", "office"):
        return WorkMode.ONSITE
    if hint == "hybrid":
        return WorkMode.HYBRID
    if hint == "remote":
        return WorkMode.REMOTE
    text = " ".join(frags)
    if HYBRID_RE.search(text):
        return WorkMode.HYBRID
    remote_flags = [bool(REMOTE_RE.search(f)) for f in frags]
    if remote_flags and all(remote_flags):
        return WorkMode.REMOTE
    if ONSITE_RE.search(text):
        return WorkMode.ONSITE
    return WorkMode.UNKNOWN  # mixed remote/office or no signal: do not guess


def classify(job: RawJob, worldwide_counts_as_us: bool = False) -> Classification:
    frags = split_locations(job)
    mode = _work_mode(job, frags)

    codes = {c.strip().upper() for c in job.country_codes if c and c.strip()}
    if codes & US_COUNTRY_CODES:
        return Classification(
            work_mode=mode, eligibility=USEligibility.US, reason="structured-country:US"
        )

    results = [(f, *classify_fragment(f)) for f in frags]
    kinds = {k for _, k, _ in results}

    if Frag.US in kinds:
        reason = next(r for _, k, r in results if k == Frag.US)
        return Classification(work_mode=mode, eligibility=USEligibility.US, reason=reason)
    if Frag.US_REGION in kinds:
        reason = next(r for _, k, r in results if k == Frag.US_REGION)
        return Classification(work_mode=mode, eligibility=USEligibility.REMOTE_US_OK, reason=reason)
    if Frag.GLOBAL in kinds and worldwide_counts_as_us:
        return Classification(
            work_mode=mode, eligibility=USEligibility.REMOTE_US_OK, reason="global-remote"
        )
    if Frag.GLOBAL in kinds or Frag.UNKNOWN in kinds or not results:
        if codes and kinds <= {Frag.NON_US, Frag.UNKNOWN} and Frag.GLOBAL not in kinds:
            # Structured non-US country with vague text (e.g. "Remote"), trust the structure.
            return Classification(
                work_mode=mode,
                eligibility=USEligibility.NON_US,
                reason=f"structured-country:{sorted(codes)[0]}",
            )
        reason = (
            "no-location"
            if not results
            else next(r for _, k, r in results if k in (Frag.GLOBAL, Frag.UNKNOWN))
        )
        return Classification(
            work_mode=mode, eligibility=USEligibility.LOCATION_UNCLEAR, reason=reason
        )
    return Classification(work_mode=mode, eligibility=USEligibility.NON_US, reason=results[0][2])


# ---------- optional attributes (unknown unless evidence) ----------
SENIORITY_RULES = [
    ("intern", re.compile(r"\b(intern|internship|co-?op)\b", re.I)),
    ("executive", re.compile(r"\b(vp|vice president|chief|cto|ceo|cfo|coo|head of)\b", re.I)),
    ("director", re.compile(r"\bdirector\b", re.I)),
    ("principal", re.compile(r"\b(principal|distinguished|fellow)\b", re.I)),
    ("staff", re.compile(r"\bstaff\b", re.I)),
    ("manager", re.compile(r"\bmanager\b", re.I)),
    ("lead", re.compile(r"\b(lead|tech lead)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?|entry[- ]level|new grad|graduate)\b", re.I)),
]
CITIZENSHIP_RE = re.compile(
    r"\b(u\.?s\.? citizen(ship)?( is)? required|must be a u\.?s\.? citizen|security clearance|"
    r"active (ts|secret|top secret)|itar)\b",
    re.I,
)
NO_SPONSOR_RE = re.compile(
    r"\b(unable to|not able to|cannot|can ?not|will not|won't|do(es)? not|no)\s+(provide\s+|offer\s+)?"
    r"(visa\s+)?sponsor(ship)?\b",
    re.I,
)
SPONSOR_RE = re.compile(
    r"\b(visa sponsorship (is )?available|will sponsor|offer(s)? visa sponsorship|h-?1b sponsorship available)\b",
    re.I,
)


def seniority(title: str) -> str:
    for label, rx in SENIORITY_RULES:
        if rx.search(title):
            return label
    return "unknown"


def citizenship(text: str | None) -> str:
    if not text:
        return "unknown"
    return "required" if CITIZENSHIP_RE.search(text) else "unknown"


def sponsorship(text: str | None) -> str:
    if not text:
        return "unknown"
    if NO_SPONSOR_RE.search(text):
        return "not_offered"
    if SPONSOR_RE.search(text):
        return "offered"
    return "unknown"
