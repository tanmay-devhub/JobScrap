import pytest

from jobmonitor.enums import USEligibility as E
from jobmonitor.enums import WorkMode as W
from jobmonitor.models import RawJob
from jobmonitor.normalize import citizenship, classify, seniority, sponsorship


def job(location="", extra=(), workplace=None, countries=()):
    return RawJob(
        source_job_id="1",
        title="t",
        location=location,
        extra_locations=list(extra),
        workplace_type=workplace,
        country_codes=list(countries),
    )


@pytest.mark.parametrize(
    "location,expected",
    [
        ("San Francisco, CA", E.US),
        ("New York City", E.US),
        ("Austin, Texas", E.US),
        ("Remote - US", E.US),
        ("Remote (United States)", E.US),
        ("US Remote", E.US),
        ("Remote, EST timezone", E.US),
        ("Hybrid - Boston", E.US),
        ("London, United Kingdom", E.NON_US),
        ("Toronto, ON", E.NON_US),
        ("Bangalore, IN", E.NON_US),  # IN is not Indiana when the city is known
        ("Remote - Canada", E.NON_US),
        ("Remote - EMEA", E.NON_US),
        ("Berlin", E.NON_US),
        ("Mexico City, Mexico", E.NON_US),
        ("Albuquerque, New Mexico", E.US),  # "Mexico" inside a US state name
        ("Remote", E.LOCATION_UNCLEAR),  # ambiguous remote
        ("Remote - Worldwide", E.LOCATION_UNCLEAR),
        ("Anywhere", E.LOCATION_UNCLEAR),
        ("", E.LOCATION_UNCLEAR),
        ("Planet Mars", E.LOCATION_UNCLEAR),
        ("Remote - Americas", E.REMOTE_US_OK),
        ("Remote, North America", E.REMOTE_US_OK),
        ("Status: US/Canada remote", E.US),
        # real-world strings observed in live boards
        ("Ontario, CAN", E.NON_US),
        ("Zürich, CH", E.NON_US),
        ("São Paulo", E.NON_US),
        ("Chicago, IL", E.US),  # IL is Illinois, never Israel
        ("Wilmington, DE", E.US),  # DE is Delaware, never Germany
        ("Remote (US)", E.US),
        ("Dublin, IE; London, UK; New York City, NY", E.US),
        ("Remote - Argentina; Brazil;Chile; Costa Rica", E.NON_US),
    ],
)
def test_eligibility(location, expected):
    assert classify(job(location)).eligibility == expected


def test_any_us_location_wins_regardless_of_hq():
    j = job("London, UK", extra=["Remote (Canada)", "New York, NY"])
    assert classify(j).eligibility == E.US


def test_split_on_pipes_and_or():
    assert classify(job("Paris, France | Seattle, WA")).eligibility == E.US
    assert classify(job("Dublin or Remote")).eligibility == E.LOCATION_UNCLEAR
    assert classify(job("Dublin or Amsterdam")).eligibility == E.NON_US


def test_structured_country_codes():
    assert classify(job("Remote", countries=["US"])).eligibility == E.US
    assert classify(job("Remote", countries=["CA"])).eligibility == E.NON_US
    # global remote stays unclear even with a non-US structured country
    assert classify(job("Remote - Worldwide", countries=["DE"])).eligibility == E.LOCATION_UNCLEAR


def test_worldwide_opt_in():
    assert (
        classify(job("Remote - Worldwide"), worldwide_counts_as_us=True).eligibility
        == E.REMOTE_US_OK
    )


@pytest.mark.parametrize(
    "workplace,location,mode",
    [
        ("OnSite", "Seattle, WA", W.ONSITE),
        ("On-Site", "Seattle, WA", W.ONSITE),
        ("hybrid", "Seattle, WA", W.HYBRID),
        ("Remote", "US", W.REMOTE),
        (None, "Hybrid - Denver", W.HYBRID),
        (None, "Remote - US", W.REMOTE),
        (None, "Remote or San Francisco", W.UNKNOWN),  # mixed: don't guess
        (None, "Chicago, IL", W.UNKNOWN),
    ],
)
def test_work_mode(workplace, location, mode):
    assert classify(job(location, workplace=workplace)).work_mode == mode


def test_optional_attributes_default_unknown():
    assert seniority("Software Engineer") == "unknown"
    assert seniority("Sr. Software Engineer") == "senior"
    assert seniority("Software Engineering Intern") == "intern"
    assert citizenship(None) == "unknown"
    assert citizenship("Must be a U.S. citizen") == "required"
    assert sponsorship("We cannot sponsor visas") == "not_offered"
    assert sponsorship("Visa sponsorship is available") == "offered"
    assert sponsorship("Great team") == "unknown"
