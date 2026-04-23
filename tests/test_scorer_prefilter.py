"""Tests for the pre-filter patterns in scorer.py.

Validated 2026-04-23 against 5,938 historical scored jobs. Any pattern
that rejects a job with LLM score >= 8 was either a clear LLM mis-score
(Junior/Intern roles scored high) or a dual-geography edge case.
"""

import pytest

from applypilot.scoring.scorer import _check_ineligible


def _job(title="Senior Software Engineer", location="Remote", description="US-remote position."):
    return {"title": title, "location": location, "full_description": description}


# ── Seniority rejects ───────────────────────────────────────────────

def test_junior_title_rejected():
    assert _check_ineligible(_job(title="Junior Software Engineer")) is not None


def test_intern_title_rejected():
    assert _check_ineligible(_job(title="Platform Engineering Intern")) is not None


def test_internship_title_rejected():
    assert _check_ineligible(_job(title="Software Engineering Internship")) is not None


def test_fresher_title_rejected():
    assert _check_ineligible(_job(title="Fresher Software Engineer")) is not None


def test_entry_level_title_rejected():
    assert _check_ineligible(_job(title="Software Engineer I - Entry Level")) is not None


def test_new_grad_title_rejected():
    assert _check_ineligible(_job(title="New Grad Software Engineer")) is not None


def test_trainee_title_rejected():
    assert _check_ineligible(_job(title="Software Engineer Trainee")) is not None


def test_apprentice_title_rejected():
    assert _check_ineligible(_job(title="Software Apprentice")) is not None


# ── Sales-adjacency rejects ─────────────────────────────────────────

def test_sales_engineer_rejected():
    assert _check_ineligible(_job(title="Senior Sales Engineer")) is not None


def test_solutions_engineer_rejected():
    assert _check_ineligible(_job(title="Solutions Engineer")) is not None


def test_presales_rejected():
    assert _check_ineligible(_job(title="Senior Presales Engineer")) is not None


def test_customer_success_engineer_rejected():
    assert _check_ineligible(_job(title="Senior Customer Success Engineer")) is not None


# ── Regional sales tags ─────────────────────────────────────────────

def test_latam_in_title_rejected():
    assert _check_ineligible(_job(title="Account Manager, LATAM")) is not None


def test_mena_in_title_rejected():
    assert _check_ineligible(_job(title="Engineering Lead, MENA")) is not None


def test_anz_in_title_rejected():
    assert _check_ineligible(_job(title="Senior DevOps Engineer, ANZ")) is not None


def test_nordics_in_title_rejected():
    assert _check_ineligible(_job(title="Staff Engineer - Nordics")) is not None


def test_only_hiring_in_title_rejected():
    assert _check_ineligible(_job(title="Only hiring in Vietnam | Senior Engineer")) is not None


# ── New non-US countries in location ────────────────────────────────

@pytest.mark.parametrize("loc", [
    "Brazil Remote Work",
    "Remote — São Paulo, Brazil",
    "Mexico City, Mexico",
    "Buenos Aires, Argentina",
    "Vietnam",
    "Remote - Japan",
    "Thailand",
    "Philippines",
    "Jakarta, Indonesia",
    "Seoul, Korea",
    "Taipei, Taiwan",
    "Cairo, Egypt",
    "Nairobi, Kenya",
    "Johannesburg, South Africa",
    "Tel Aviv, Israel",
    "Istanbul, Turkey",
    "Lisbon, Portugal",
    "Dublin, Ireland",
    "Copenhagen, Denmark",
    "Stockholm, Sweden",
    "Oslo, Norway",
    "Helsinki, Finland",
    "Brussels, Belgium",
    "Zurich, Switzerland",
    "Vienna, Austria",
    "Bucharest, Romania",
    "Budapest, Hungary",
])
def test_non_us_country_in_location_rejected(loc):
    assert _check_ineligible(_job(location=loc)) is not None


# ── Must NOT reject legit US roles ──────────────────────────────────

def test_senior_us_remote_not_rejected():
    assert _check_ineligible(_job(title="Senior Software Engineer", location="Remote (US)")) is None


def test_staff_engineer_not_rejected():
    assert _check_ineligible(_job(title="Staff Software Engineer", location="Seattle, WA")) is None


def test_principal_engineer_not_rejected():
    assert _check_ineligible(_job(title="Principal Platform Engineer", location="San Francisco, CA")) is None


def test_senior_with_global_office_mention_not_rejected():
    """A US role mentioning a global office in the description should NOT be rejected.

    The description pre-filter is narrow — requires explicit regional restrictions
    like 'Remote (Europe)' or 'EMEA only', not a casual office mention.
    """
    desc = "We're a US-based company with offices in San Francisco, London, and Tokyo. "
    desc += "This role is US-remote. You'll work on distributed systems."
    assert _check_ineligible(_job(description=desc)) is None


def test_associate_director_not_rejected():
    """Ensure 'Associate' doesn't falsely match 'Entry Level'-like patterns."""
    assert _check_ineligible(_job(title="Associate Director of Engineering")) is None
