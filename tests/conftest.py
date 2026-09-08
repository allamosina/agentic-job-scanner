import os

import pytest
from sqlalchemy.engine import make_url

from job_monitor.config import Settings, load_preferences
from job_monitor.db import database, sessions
from job_monitor.evaluation import Assessment
from job_monitor.models import Base
from job_monitor.sources import JobData


@pytest.fixture
def settings():
    return Settings(telegram_user_id=123, telegram_chat_id=123, openai_model="test-model")


@pytest.fixture
def preferences(settings):
    return load_preferences(settings)


@pytest.fixture
def job_data():
    return JobData(
        company="Example",
        title="Web Strategy Lead",
        location="Prague",
        url="https://example.com/jobs/42",
        official_url="https://example.com/jobs/42",
        ats_id="greenhouse:42",
        description="Own the website roadmap. Run conversion experiments. Work with Engineering.",
        source_id="example",
        discovered_url="https://example.com/jobs/42",
    )


@pytest.fixture
def assessment():
    dim = dict(fraction=0.95, rationale="Supported by provided data", confidence="high")
    return Assessment.model_validate(
        dict(
            track="CORE_WEB",
            duties=[
                dict(
                    responsibility="Roadmap",
                    jd_quote="Own the website roadmap.",
                    importance=3,
                    evidence_ids=["cv-1"],
                    match="direct",
                )
            ],
            recruiter_fit="yes",
            normal_onboarding="yes",
            hard_exclusions=[],
            specialist_career_missing=False,
            unrelated_product=False,
            staff_principal_pm=False,
            extremely_close_product_domain=False,
            jd_accepts_equivalent_adjacent_experience=False,
            english_work_possible="yes",
            employment_feasibility="UNKNOWN",
            employment_model="employee",
            employment_proof=[],
            geography_fit="Prague",
            career_transition_realism="Direct experience",
            company_quality="UNKNOWN",
            company_proof=[],
            compensation_fit="UNKNOWN",
            compensation_summary="Not published",
            dimensions={
                k: dim.copy()
                for k in ["responsibility", "seniority", "employment", "compensation", "company", "freshness"]
            },
            why_it_fits=["Website roadmap ownership"],
            gaps=["Salary unknown"],
            recommended_action="Research",
            pmm=None,
        )
    )


@pytest.fixture
def factory():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not configured; requires isolated PostgreSQL")
    name = make_url(url).database
    if not name or not name.endswith("_test"):
        pytest.fail("Refusing to touch database not ending in _test")
    engine = database(Settings(database_url=url))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield sessions(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()
