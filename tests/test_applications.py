from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import yaml
from sqlalchemy import func, select
from test_postgres import prepare

from job_monitor.applications import (
    apply_snapshot,
    blocked_job_ids,
    fetch_snapshot,
    freshness_problem,
    link_application,
    load_notion,
    normalize_page,
    outcome_report,
    sync_notion,
    url_key,
)
from job_monitor.config import fingerprint
from job_monitor.delivery import build_queue, dispatch
from job_monitor.models import ApplicationEvent, Delivery, Feedback, NotionApplication, State, utcnow
from job_monitor.store import save_job


def page(
    status="Applied",
    number=1,
    url="https://example.com/jobs/42",
    company="Example",
    channel="Company website",
):
    return {
        "object": "page",
        "id": str(UUID(int=number)),
        "properties": {
            "Company": {"type": "title", "title": [{"plain_text": company}]},
            "Job description": {"type": "url", "url": url},
            "Status": {"type": "status", "status": {"name": status}},
            "Date": {"type": "date", "date": {"start": "2026-08-20"}},
            "Type": {"type": "select", "select": {"name": channel}},
            "Comment": {"type": "rich_text", "rich_text": []},
        },
    }


def row(settings, **kwargs):
    return normalize_page(page(**kwargs), load_notion(settings))


def save_rows(factory, settings, rows, now=None):
    with factory.begin() as session:
        return apply_snapshot(session, rows, load_notion(settings), now or utcnow())


@pytest.mark.parametrize(
    "left,right",
    [
        (
            "https://www.linkedin.com/jobs/view/example-job-at-example-1000000001/",
            "https://www.linkedin.com/jobs/view/1000000001/?trackingId=test",
        ),
        (
            "https://job-boards.greenhouse.io/make/jobs/1000000002",
            "https://www.make.com/en/careers-detail?gh_jid=1000000002",
        ),
        (
            "https://jobs.ashbyhq.com/test/11111111-1111-4111-8111-111111111111?utm_source=x",
            "https://jobs.ashbyhq.com/test/11111111-1111-4111-8111-111111111111/application",
        ),
        (
            "https://example.teamtailor.com/jobs/1000003-web-growth-product-owner?utm_source=LinkedIn",
            "https://example.teamtailor.com/jobs/1000003-new-title",
        ),
    ],
)
def test_job_identity(left, right):
    assert url_key(left) == url_key(right) is not None


@pytest.mark.parametrize(
    "value",
    [
        "Associate",
        "Product marketing manager (outreach)",
        "",
        "https://example.com/careers",
        "https://www.linkedin.com/jobs/search/",
        "javascript:alert(1)",
    ],
)
def test_no_invented_url_identity(value):
    assert url_key(value) is None


def test_unknown_status_and_plain_role(settings):
    r = row(settings, status="New custom status", url="Associate")
    assert r["applied"] is None and r["stage"] is None and r["url_key"] is None
    assert row(settings, status="Rejected")["stage"] is None
    assert row(settings, status="Not started")["applied"] is False
    assert row(settings, status="Manager interviewes scheduled")["stage"] == 3


async def test_pagination_and_read_only(settings):
    requests = []
    schema = {name: {"type": prop["type"]} for name, prop in page()["properties"].items()}

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"properties": schema})
        assert request.url.path.endswith("/query")
        if len(requests) == 2:
            return httpx.Response(200, json={"results": [page()], "has_more": True, "next_cursor": "next"})
        assert b'"start_cursor":"next"' in request.content
        return httpx.Response(200, json={"results": [page(number=2)], "has_more": False})

    async with httpx.AsyncClient(
        base_url="https://api.notion.com", transport=httpx.MockTransport(handler)
    ) as client:
        rows = await fetch_snapshot(client, load_notion(settings))
    assert len(rows) == 2 and len(requests) == 3


def test_exact_applied_not_company_block(factory, settings, job_data):
    with factory.begin() as session:
        first, _, _ = save_job(session, job_data)
        second, _, _ = save_job(
            session,
            job_data.model_copy(
                update={
                    "url": "https://example.com/jobs/43",
                    "official_url": "https://example.com/jobs/43",
                    "ats_id": "greenhouse:43",
                }
            ),
        )
    save_rows(
        factory,
        settings,
        [
            row(settings, status="Rejected"),
            row(settings, number=2, status="Not started", url="https://example.com/jobs/43"),
        ],
    )
    with factory() as session:
        assert blocked_job_ids(session, 123) == {first}
        assert second not in blocked_job_ids(session, 123)


def test_snapshot_idempotence_and_rejected_after_hr(factory, settings):
    original = row(settings, status="HR interview happened")
    assert save_rows(factory, settings, [original])["changes"] == 1
    assert save_rows(factory, settings, [original])["changes"] == 0
    save_rows(factory, settings, [row(settings, status="Rejected")])
    with factory() as session:
        app = session.scalar(select(NotionApplication))
        assert app.max_observed_stage == 2
        assert session.scalar(select(func.count()).select_from(ApplicationEvent)) == 2
        group = outcome_report(session, settings)["groups"][0]
        assert group["rejected"] == 1 and group["progressed"] == 1
        assert group["rejected_stage_unknown"] == 0
    save_rows(factory, settings, [original])
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ApplicationEvent)) == 3


def test_old_reject_unknown_and_missing_keeps_block(factory, settings, job_data):
    with factory.begin() as session:
        jid, _, _ = save_job(session, job_data)
    save_rows(factory, settings, [row(settings, status="Rejected")])
    with factory() as session:
        assert outcome_report(session, settings)["groups"][0]["rejected_stage_unknown"] == 1
    save_rows(factory, settings, [])
    with factory() as session:
        assert session.scalar(select(NotionApplication)).missing
        assert blocked_job_ids(session, 123) == {jid}


async def test_failed_second_page_keeps_previous_snapshot(factory, settings):
    settings = settings.model_copy(update={"notion_sync_enabled": True, "notion_api_key": "test"})
    save_rows(factory, settings, [row(settings)])
    schema = {name: {"type": prop["type"]} for name, prop in page()["properties"].items()}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"properties": schema})
        if b"start_cursor" in request.content:
            return httpx.Response(429, json={})
        return httpx.Response(
            200, json={"results": [page(status="Not started")], "has_more": True, "next_cursor": "next"}
        )

    async with httpx.AsyncClient(
        base_url="https://api.notion.com", transport=httpx.MockTransport(handler)
    ) as client:
        result = await sync_notion(factory, settings, client=client)
    assert result["status"] == "failed"
    with factory() as session:
        assert session.scalar(select(NotionApplication)).snapshot["applied"]
        assert session.scalar(select(func.count()).select_from(ApplicationEvent)) == 1
        assert freshness_problem(session, settings)


async def test_daily_gate_and_new_local_day(factory, settings):
    from datetime import UTC, datetime

    settings = settings.model_copy(update={"notion_sync_enabled": True, "notion_api_key": "test"})
    schema = {name: {"type": prop["type"]} for name, prop in page()["properties"].items()}
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"properties": schema}
            if request.method == "GET"
            else {"results": [page()], "has_more": False},
        )

    async with httpx.AsyncClient(
        base_url="https://api.notion.com", transport=httpx.MockTransport(handler)
    ) as client:
        # Prague summer time: local 23:30 -> 00:30, despite same UTC calendar date.
        now = datetime(2026, 9, 8, 21, 30, tzinfo=UTC)
        assert (await sync_notion(factory, settings, client=client, now=now))["status"] == "complete"
        assert (await sync_notion(factory, settings, client=client, now=now + timedelta(minutes=5)))[
            "status"
        ] == "current_today"
        assert len(calls) == 2
        assert (await sync_notion(factory, settings, client=client, now=now + timedelta(hours=1)))[
            "status"
        ] == "complete"
        assert len(calls) == 4


def test_ambiguous_urls_require_explicit_link(factory, settings, job_data):
    with factory.begin() as session:
        jid, _, _ = save_job(session, job_data)
    rows = [row(settings), row(settings, number=2, company="Different")]
    save_rows(factory, settings, rows)
    with factory.begin() as session:
        assert blocked_job_ids(session, 123) == set()
        assert all(a["conflicting_url"] for a in outcome_report(session, settings)["applications"])
        link_application(session, rows[0]["page_id"], jid)
    with factory() as session:
        assert blocked_job_ids(session, 123) == {jid}
        assert outcome_report(session, settings)["groups"] == []


@pytest.mark.parametrize("during_verification", [False, True])
async def test_applied_after_queue_is_not_sent(
    factory, settings, preferences, job_data, assessment, during_verification
):
    jid = prepare(factory, job_data, assessment, settings, preferences)
    build_queue(factory, settings, preferences, "2026-09-08T08:00")

    def applied():
        with factory.begin() as session:
            session.add(Feedback(job_id=jid, user_id="123", action="applied", event_key="1"))

    if not during_verification:
        applied()

    class Web:
        async def get(self, url):
            applied()
            return httpx.Response(200, text="active", request=httpx.Request("GET", url))

    class Bot:
        messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs["text"])
            return SimpleNamespace(message_id=1)

    bot = Bot()
    await dispatch(factory, bot, Web(), settings)
    assert len(bot.messages) == 0  # Scheduled diagnostic reports are suppressed.
    with factory() as session:
        assert (
            session.scalar(select(Delivery.status).where(Delivery.version_id.is_not(None)))
            == "already_applied"
        )


def test_stale_notion_defers_queue(factory, settings, preferences, job_data, assessment):
    settings = settings.model_copy(update={"notion_sync_enabled": True})
    prepare(factory, job_data, assessment, settings, preferences)
    build_queue(factory, settings, preferences, "2026-09-08T08:00")
    with factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(Delivery).where(Delivery.version_id.is_not(None)))
            == 0
        )
        assert session.scalar(select(Delivery.status)) == "suppressed"
    config = load_notion(settings)
    with factory.begin() as session:
        session.merge(
            State(
                key="notion_sync",
                value={
                    "data_source_id": config.data_source_id,
                    "success_config_hash": fingerprint(config),
                    "last_success": (utcnow() - timedelta(hours=37)).isoformat(),
                },
            )
        )
    with factory() as session:
        assert freshness_problem(session, settings)


def test_outcomes_do_not_make_pending_negative_or_mix_channels(factory, settings, tmp_path):
    config = load_notion(settings).model_dump()
    rows = []
    for i in range(1, 16):
        status = "Rejected" if i <= 5 else ("Offered" if i <= 10 else "Applied")
        rows.append(row(settings, number=i, status=status, url=f"https://example.com/jobs/{i}"))
        config["track_overrides"][str(UUID(int=i))] = "CORE_WEB" if i <= 5 or i > 10 else "GROWTH"
    path = tmp_path / "notion.yaml"
    path.write_text(yaml.safe_dump(config))
    settings = settings.model_copy(update={"notion_config_path": str(path)})
    save_rows(factory, settings, rows)
    with factory() as session:
        report = outcome_report(session, settings)
        assert -2 <= report["adjustments"]["CORE_WEB"] < 0
        assert 0 < report["adjustments"]["GROWTH"] <= 2
        before = report["adjustments"]
    # Pending samples do not affect either resolved conversion denominator or confirmed progress.
    save_rows(factory, settings, rows[:10])
    with factory() as session:
        assert outcome_report(session, settings)["adjustments"] == before
    for r in rows[5:10]:
        r["channel"] = "Recommendation"
    save_rows(factory, settings, rows)
    with factory() as session:
        report = outcome_report(session, settings)
        assert "CORE_WEB" not in report["adjustments"]
        assert report["adjustments"]["GROWTH"] > 0  # observed offers/progress only


def test_progress_and_duplicate_rows(factory, settings, tmp_path):
    config = load_notion(settings).model_dump()
    rows = [
        row(settings, number=i, status="Manager interview happened", url=f"https://example.com/jobs/{i}")
        for i in range(1, 6)
    ]
    config["track_overrides"] = {r["page_id"]: "CORE_WEB" for r in rows}
    path = tmp_path / "notion.yaml"
    path.write_text(yaml.safe_dump(config))
    settings = settings.model_copy(update={"notion_config_path": str(path)})
    save_rows(factory, settings, rows)
    with factory() as session:
        assert 0 < outcome_report(session, settings)["adjustments"]["CORE_WEB"] <= 0.5
    rows.append({**rows[0], "page_id": str(UUID(int=9))})
    save_rows(factory, settings, rows)
    with factory() as session:
        report = outcome_report(session, settings)
        assert report["adjustments"] == {}
        assert sum(a["duplicate_url"] for a in report["applications"]) == 2


def test_reusing_page_does_not_transfer_old_progress(factory, settings):
    save_rows(factory, settings, [row(settings, status="Manager interview happened")])
    save_rows(factory, settings, [row(settings, status="Rejected", url="https://example.com/jobs/99")])
    with factory() as session:
        assert session.scalar(select(NotionApplication)).max_observed_stage is None
        assert session.scalar(select(func.count()).select_from(ApplicationEvent)) == 2


async def test_failed_forced_sync_retries_even_after_success_today(factory, settings):
    settings = settings.model_copy(update={"notion_sync_enabled": True, "notion_api_key": "test"})
    config = load_notion(settings)
    now = utcnow()
    with factory.begin() as session:
        session.add(
            State(
                key="notion_sync",
                value={
                    "status": "failed",
                    "data_source_id": config.data_source_id,
                    "config_hash": fingerprint(config),
                    "success_config_hash": fingerprint(config),
                    "last_success": now.isoformat(),
                    "last_attempt": now.isoformat(),
                },
            )
        )
    calls = []
    schema = {name: {"type": prop["type"]} for name, prop in page()["properties"].items()}

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"properties": schema}
            if request.method == "GET"
            else {"results": [page()], "has_more": False},
        )

    async with httpx.AsyncClient(
        base_url="https://api.notion.com", transport=httpx.MockTransport(handler)
    ) as client:
        assert (await sync_notion(factory, settings, client=client, now=now + timedelta(minutes=5)))[
            "status"
        ] == "failed"
        assert not calls
        assert (await sync_notion(factory, settings, client=client, now=now + timedelta(minutes=31)))[
            "status"
        ] == "complete"
        assert len(calls) == 2
