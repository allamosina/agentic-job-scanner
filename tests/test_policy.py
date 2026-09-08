import copy
from datetime import datetime

import pytest
from pydantic import ValidationError

from job_monitor.config import Schedule
from job_monitor.delivery import material_change, slot_now
from job_monitor.evaluation import PMM, Rejection, classify, score_assessment, validate_evidence


@pytest.mark.parametrize(
    "score,expected",
    [
        (100, "APPLY NOW"),
        (90, "APPLY NOW"),
        (89, "STRONG MATCH"),
        (80, "STRONG MATCH"),
        (79, "REVIEW"),
        (70, "REVIEW"),
        (69, "STRETCH"),
        (60, "STRETCH"),
        (59, "SKIP"),
    ],
)
def test_classification(score, expected):
    assert classify(score) == expected


def test_hard_exclusion_cannot_be_bought_by_score(assessment, preferences):
    assessment.hard_exclusions = [
        Rejection(kind="language", reason="Czech mandatory", jd_quote="Czech required")
    ]
    result = score_assessment(assessment, preferences.weights)
    assert not result["eligible"]
    assert result["category"] == "SKIP"


def test_unrelated_pm_rejected(assessment, preferences):
    assessment.track, assessment.unrelated_product = "PRODUCT", True
    assert not score_assessment(assessment, preferences.weights)["eligible"]


def test_staff_pm_requires_both_conditions(assessment, preferences):
    assessment.staff_principal_pm = True
    assessment.extremely_close_product_domain = True
    assert not score_assessment(assessment, preferences.weights)["eligible"]
    assessment.jd_accepts_equivalent_adjacent_experience = True
    assert score_assessment(assessment, preferences.weights)["eligible"]


def test_pmm_requires_company_gate_not_past_title(assessment, preferences):
    assessment.track = "EMERGING_PRODUCT_MARKETING"
    assessment.pmm = PMM(
        why_pmm_transition_is_credible="Product journeys",
        hard_pmm_experience_required=False,
        case_study_potential="high",
        suggested_case_angle="Activation",
    )
    assert not score_assessment(assessment, preferences.weights)["eligible"]
    assessment.company_quality = "STRONG"
    assert score_assessment(assessment, preferences.weights)["eligible"]
    assessment.pmm.hard_pmm_experience_required = True
    assert not score_assessment(assessment, preferences.weights)["eligible"]


def test_salary_and_geography_unknown_do_not_reject(assessment, preferences):
    assert assessment.employment_feasibility == "UNKNOWN"
    assert assessment.compensation_fit == "UNKNOWN"
    assert score_assessment(assessment, preferences.weights)["eligible"]


def test_low_salary_not_hard_exclusion(assessment, preferences):
    assessment.compensation_fit = "LOW"
    assessment.dimensions.compensation.fraction = 0
    assert score_assessment(assessment, preferences.weights)["eligible"]


def test_missing_specialist_career_caps_score(assessment, preferences):
    assessment.specialist_career_missing = True
    assert score_assessment(assessment, preferences.weights)["score"] <= 69


def test_two_core_gaps_cap_79(assessment, preferences):
    for _ in range(2):
        duty = assessment.duties[0].model_copy(update={"match": "gap", "evidence_ids": [], "importance": 1})
        assessment.duties.append(duty)
    assert score_assessment(assessment, preferences.weights)["score"] == 79


def test_fabricated_evidence_is_rejected(assessment, job_data):
    with pytest.raises(ValueError, match="Unknown candidate"):
        validate_evidence(assessment, job_data.model_dump(), {"evidence": []}, [])


def test_exact_jd_quote_required(assessment, job_data):
    assessment.duties[0].jd_quote = "Invented roadmap requirement"
    with pytest.raises(ValueError, match="quotation"):
        validate_evidence(assessment, job_data.model_dump(), {"evidence": [{"id": "cv-1"}]}, [])


@pytest.mark.parametrize(
    "utc_hour,date", [(6, "2026-09-08"), (7, "2026-12-08"), (6, "2026-03-29"), (7, "2026-10-25")]
)
def test_prague_daylight_saving(utc_hour, date, preferences):
    now = datetime.fromisoformat(f"{date}T{utc_hour:02}:00:15+00:00")
    assert slot_now(now, preferences.schedule) == date + "T08:00"


def test_no_unspecified_delivery(preferences):
    assert slot_now(datetime.fromisoformat("2026-09-08T07:00:00+00:00"), preferences.schedule) is None


def test_duplicate_slots_invalid():
    with pytest.raises(ValidationError):
        Schedule(timezone="Europe/Prague", delivery_times=["08:00", "08:00"])


def test_salary_added_is_meaningful(job_data):
    old = job_data.model_dump()
    new = copy.deepcopy(old)
    new["salary"] = {"min": 90000}
    assert material_change(old, new)


def test_minor_jd_edit_does_not_repeat(job_data):
    old = job_data.model_dump()
    new = copy.deepcopy(old)
    new["description"] += " "
    assert not material_change(old, new)
