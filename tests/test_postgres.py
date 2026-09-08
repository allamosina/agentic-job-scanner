from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import httpx
from sqlalchemy import func, select
from telegram.error import NetworkError

from job_monitor.config import fingerprint, load_profile
from job_monitor.delivery import build_queue, dispatch
from job_monitor.evaluation import score_assessment
from job_monitor.models import Delivery, Evaluation, Feedback, Job, Observation, State, Version, utcnow
from job_monitor.store import BudgetUnavailable, feedback_boost, feedback_summary, reserve_call, save_job


def prepare(factory, job_data, assessment, settings, preferences):
    with factory.begin() as session:
        jid, _, _ = save_job(session, job_data)
        version = session.scalar(select(Version).where(Version.job_id == jid))
        scored = score_assessment(assessment, preferences.weights)
        result = {**assessment.model_dump(), **scored}
        evaluation = Evaluation(
            version_id=version.id,
            policy_hash=fingerprint(preferences),
            profile_hash=fingerprint(load_profile(settings)),
            model=settings.openai_model,
            score=result["score"],
            category=result["category"],
            eligible=result["eligible"],
            result=result,
        )
        session.add(evaluation)
    return jid


def test_empty_then_deduplicated(factory, job_data):
    with factory.begin() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        jid, new, changed = save_job(session, job_data)
        assert new and changed
        same, new, changed = save_job(session, job_data)
        assert jid == same and not new and not changed
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Version)) == 1


def test_same_board_page_multiple_jobs_keep_provenance(factory, job_data):
    job_data.discovered_url = "https://example.com/jobs"
    other = job_data.model_copy(
        update={
            "ats_id": "greenhouse:43",
            "url": "https://example.com/jobs/43",
            "official_url": "https://example.com/jobs/43",
        }
    )
    with factory.begin() as session:
        save_job(session, job_data)
        save_job(session, other)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Observation)) == 2


def test_no_feedback_means_no_bias(factory):
    with factory() as session:
        assert feedback_boost(session, 123, "CORE_WEB", {}) == 0


def test_feedback_reset_preserves_history(factory, job_data):
    with factory.begin() as session:
        jid, _, _ = save_job(session, job_data)
        session.add(
            Feedback(
                job_id=jid,
                user_id="123",
                action="like",
                event_key="1",
                created_at=utcnow() - timedelta(minutes=1),
            )
        )
    with factory() as session:
        assert feedback_summary(session, 123) == {jid: "like"}
    with factory.begin() as session:
        session.add(State(key="learning_reset", value={"at": utcnow().isoformat()}))
    with factory() as session:
        assert feedback_summary(session, 123) == {}
        assert session.scalar(select(func.count()).select_from(Feedback)) == 1


def test_atomic_budget_cap(factory):
    def attempt(_):
        try:
            reserve_call(factory, "test", 1)
            return True
        except BudgetUnavailable:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(attempt, range(2))) == 1


def test_digest_idempotent(factory, job_data, assessment, settings, preferences):
    prepare(factory, job_data, assessment, settings, preferences)
    build_queue(factory, settings, preferences, "2026-09-08T08:00")
    build_queue(factory, settings, preferences, "2026-09-08T08:00")
    build_queue(factory, settings, preferences, "2026-09-08T13:00")
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(Delivery).where(Delivery.version_id.is_not(None)))
            == 1
        )
        assert (
            session.scalar(select(func.count()).select_from(Delivery).where(Delivery.kind == "summary")) == 2
        )


class WebOK:
    async def get(self, url):
        return httpx.Response(200, text="Active job", request=httpx.Request("GET", url))


async def test_ambiguous_send_is_not_retried(factory, job_data, assessment, settings, preferences):
    prepare(factory, job_data, assessment, settings, preferences)
    build_queue(factory, settings, preferences, "2026-09-08T08:00")

    class Bot:
        calls = 0

        async def send_message(self, **kwargs):
            self.calls += 1
            raise NetworkError("Timeout after request may have been sent")

    bot = Bot()
    await dispatch(factory, bot, WebOK())
    calls = bot.calls
    await dispatch(factory, bot, WebOK())
    assert bot.calls == calls
    with factory() as session:
        assert set(session.scalars(select(Delivery.status))) == {"unknown"}


async def test_closed_job_not_sent(factory, job_data, assessment, settings, preferences):
    prepare(factory, job_data, assessment, settings, preferences)
    build_queue(factory, settings, preferences, "2026-09-08T08:00")

    class Closed:
        async def get(self, url):
            httpx.Response(404, request=httpx.Request("GET", url)).raise_for_status()

    class Bot:
        messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs["text"])
            return SimpleNamespace(message_id=1)

    bot = Bot()
    await dispatch(factory, bot, Closed())
    assert len(bot.messages) == 1  # audit summary only
    with factory() as session:
        assert session.scalar(select(Job.active)) is False


def test_latest_rejection_supersedes_old_match(factory, job_data, assessment, settings, preferences):
    prepare(factory, job_data, assessment, settings, preferences)
    with factory.begin() as session:
        prior = session.scalar(select(Evaluation))
        result = {**prior.result, "eligible": False, "reasons": ["functional_fit: new evidence"]}
        session.add(
            Evaluation(
                version_id=prior.version_id,
                policy_hash=prior.policy_hash,
                profile_hash=prior.profile_hash,
                model=prior.model,
                score=40,
                category="SKIP",
                eligible=False,
                result=result,
                created_at=prior.created_at + timedelta(seconds=1),
            )
        )
    build_queue(factory, settings, preferences, "2026-09-08T08:00")
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(Delivery).where(Delivery.version_id.is_not(None)))
            == 0
        )


async def test_verification_failure_retries_later_without_sending_job(
    factory, job_data, assessment, settings, preferences
):
    prepare(factory, job_data, assessment, settings, preferences)
    build_queue(factory, settings, preferences, "2026-09-08T08:00")

    class Unavailable:
        async def get(self, url):
            raise httpx.ConnectError("temporary failure")

    class Bot:
        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=1)

    await dispatch(factory, Bot(), Unavailable())
    with factory() as session:
        item = session.scalar(select(Delivery).where(Delivery.version_id.is_not(None)))
        assert item.status == "pending"
        assert item.next_attempt_at > utcnow()
        assert item.verification_attempts == 1
