from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select

from .applications import blocked_job_ids, freshness_problem, sync_notion
from .config import Source, fingerprint, load_preferences, load_profile, load_sources
from .db import transaction_lock
from .evaluation import assess
from .models import Evaluation, Job, Observation, Run, State, Version, utcnow
from .research import company_research, discover, find_signals, official_job
from .sources import Web, fetch_source
from .store import BudgetUnavailable, save_job

log = logging.getLogger(__name__)


def source_due(factory, source, hours):
    with factory() as session:
        last = session.scalar(
            select(Run)
            .where(Run.source_id == source.id, Run.status.in_(["complete", "partial"]))
            .order_by(Run.started_at.desc())
            .limit(1)
        )
        return last is None or last.started_at < utcnow() - timedelta(hours=hours)


def discovery_priority(title):
    # Scheduling priority only: no vacancy is discarded using this title heuristic.
    value = title.casefold()
    if any(
        s in value
        for s in [
            "web",
            "digital experience",
            "cro",
            "conversion",
            "experimentation",
            "personalisation",
            "personalization",
            "product marketing",
            "gtm strategy",
        ]
    ):
        return 0
    if any(s in value for s in ["product", "growth", "marketing", "digital", "consult", "delivery"]):
        return 1
    return 2


async def collect_one(factory, settings, preferences, web, source):
    with factory.begin() as session:
        run = Run(source_id=source.id)
        session.add(run)
        session.flush()
        run_id = run.id
    counts = {"raw": 0, "new": 0, "changed": 0, "duplicates": 0}
    status, error = "failed", None
    try:
        async with asyncio.timeout(preferences.operations.source_timeout_seconds):
            result = (
                await discover(factory, settings, web, source, preferences.operations.max_pages_per_source)
                if source.kind == "search"
                else await fetch_source(web, source, preferences.operations.max_pages_per_source)
            )
        seen = set()
        for item in result.jobs:
            counts["raw"] += 1
            # Optional official enrichment only when paid search is enabled. Keep discovery if unavailable.
            if not item.official_url and discovery_priority(item.title) == 0 and settings.paid_apis_enabled:
                try:
                    item = await official_job(factory, settings, web, item)
                except (BudgetUnavailable, httpx.HTTPError, ValueError):
                    pass
            with factory.begin() as session:
                job_id, new, changed = save_job(session, item)
            seen.add(job_id)
            counts["new"] += int(new)
            counts["changed"] += int(changed and not new)
            counts["duplicates"] += int(not new and not changed)
        # Only a complete employer API snapshot may close missing jobs.
        if result.complete and source.employer_source:
            with factory.begin() as session:
                for job in session.scalars(
                    select(Job).join(Observation).where(Observation.source_id == source.id)
                ):
                    if job.id not in seen:
                        job.active = False
        status = "complete" if result.complete else ("partial" if result.jobs else "unavailable")
        error = result.note or None
    except BudgetUnavailable as exc:
        status, error = "disabled", str(exc)
    except (TimeoutError, ValueError, httpx.HTTPError, KeyError, TypeError) as exc:
        # Don't persist URLs with tokens or raw provider exception bodies.
        error = type(exc).__name__ + ": fetch did not finish or returned unsupported data"
    except asyncio.CancelledError:
        status, error = "interrupted", "Scan deadline reached"
        raise
    finally:
        with factory.begin() as session:
            run = session.get(Run, run_id)
            run.status, run.error, run.counts, run.ended_at = status, error, counts, utcnow()
    log.info("source=%s status=%s raw=%s new=%s", source.id, status, counts["raw"], counts["new"])
    return counts


async def evaluate_pending(factory, settings, preferences, web):
    if not settings.paid_apis_enabled or not settings.openai_model or not settings.openai_api_key:
        return {"evaluated": 0, "evaluation_state": "disabled"}
    profile = load_profile(settings)
    from .compensation import exchange_rates

    fx = None
    try:
        fx = await exchange_rates(web)
    except Exception as exc:
        log.warning("currency normalization unavailable: %s", type(exc).__name__)
    policy_hash, profile_hash = fingerprint(preferences), fingerprint(profile)
    with factory() as session:
        if freshness_problem(session, settings):
            return {"evaluated": 0, "evaluation_state": "waiting_for_notion"}
        blocked = blocked_job_ids(session, settings.telegram_user_id)
        rows = session.execute(
            select(Job, Version)
            .join(Version, Version.job_id == Job.id)
            .where(Job.active.is_(True), Version.content_hash == Job.current_hash)
        ).all()
        known = set(
            session.scalars(
                select(Evaluation.version_id).where(
                    Evaluation.policy_hash == policy_hash,
                    Evaluation.profile_hash == profile_hash,
                    Evaluation.model == settings.openai_model,
                    Evaluation.created_at
                    >= utcnow() - timedelta(hours=preferences.operations.research_ttl_hours),
                )
            )
        )
        pending = [
            (job, version) for job, version in rows if version.id not in known and job.id not in blocked
        ]
    pending.sort(key=lambda pair: (discovery_priority(pair[0].title), pair[0].first_seen))
    completed, failed = 0, 0
    for job, version in pending[: preferences.operations.max_evaluations_per_run]:
        research = []
        try:
            research = await company_research(
                factory, settings, web, job.company, preferences.operations.research_ttl_hours
            )
        except (BudgetUnavailable, httpx.HTTPError, ValueError):
            pass
        try:
            result = await assess(factory, settings, preferences, profile, version.payload, research, fx)
            with factory.begin() as session:
                session.add(
                    Evaluation(
                        version_id=version.id,
                        policy_hash=policy_hash,
                        profile_hash=profile_hash,
                        model=settings.openai_model,
                        result=result,
                        score=result["score"],
                        category=result["category"],
                        eligible=result["eligible"],
                    )
                )
            completed += 1
        except BudgetUnavailable:
            break
        except Exception as exc:
            failed += 1
            log.warning("evaluation deferred job=%s error=%s", job.id, type(exc).__name__)
    return {"evaluated": completed, "evaluation_errors": failed, "pending_before_run": len(pending)}


def watchlist_sources(preferences, configured):
    monitored = {s.company.casefold() for s in configured if s.company and s.enabled}
    companies = list(
        dict.fromkeys(
            c
            for values in preferences.watchlists.values()
            for c in values
            if not c.lower().startswith("other ")
        )
    )
    return [
        Source(
            id="watch-" + fingerprint(company)[:12],
            kind="search",
            company=company,
            query=f'"{company}" careers jobs (web OR marketing OR product OR digital)',
            tier="1",
        )
        for company in companies
        if company.casefold() not in monitored
    ]


async def scan(factory, settings, force=False):
    preferences = load_preferences(settings)
    notion = await sync_notion(factory, settings)
    with transaction_lock(factory, "scan") as guard:
        if guard is None:
            return {"state": "already_running"}
        web = Web()
        totals = {"raw": 0, "new": 0, "changed": 0, "duplicates": 0, "notion": notion}
        try:
            async with asyncio.timeout(preferences.operations.scan_timeout_seconds):
                configured = load_sources(settings)
                sources = configured + watchlist_sources(preferences, configured)
                sources += [
                    Source(id=f"broad-{i}", kind="search", tier="2", query=q)
                    for i, q in enumerate(preferences.discovery_queries)
                ]
                with factory.begin() as session:
                    session.merge(
                        State(
                            key="coverage_plan",
                            value={
                                "enabled_sources": sum(s.enabled for s in sources),
                                "at": utcnow().isoformat(),
                            },
                        )
                    )
                # Rotate failed/unchecked sources too so a tight budget doesn't starve the end of the list.
                with factory() as session:
                    state = session.get(State, "search_cursor")
                    cursor = state.value.get("index", 0) if state else 0
                free = [s for s in sources if s.kind != "search"]
                paid = [s for s in sources if s.kind == "search"]
                if paid:
                    cursor %= len(paid)
                    paid = paid[cursor:] + paid[:cursor]
                attempted_searches = 0
                for source in free + paid:
                    if not source.enabled:
                        continue
                    if (
                        source.kind == "search"
                        and attempted_searches >= preferences.operations.search_sources_per_run
                    ):
                        break
                    if not force and not source_due(
                        factory, source, preferences.operations.source_interval_hours[source.tier]
                    ):
                        continue
                    counts = await collect_one(factory, settings, preferences, web, source)
                    for key in counts:
                        totals[key] += counts[key]
                    attempted_searches += int(source.kind == "search")
                with factory.begin() as session:
                    session.merge(
                        State(key="search_cursor", value={"index": cursor + max(1, attempted_searches)})
                    )
                totals.update(await evaluate_pending(factory, settings, preferences, web))
                if settings.paid_apis_enabled:
                    companies = list(
                        dict.fromkeys(
                            c
                            for values in preferences.watchlists.values()
                            for c in values
                            if not c.lower().startswith("other ")
                        )
                    )
                    with factory() as session:
                        state = session.get(State, "outbound_cursor")
                        start = state.value.get("index", 0) if state else 0
                    totals["outbound_signals"] = 0
                    for i in range(preferences.operations.outbound_companies_per_run):
                        if not companies:
                            break
                        company = companies[(start + i) % len(companies)]
                        try:
                            totals["outbound_signals"] += await find_signals(
                                factory, settings, web, preferences, company
                            )
                        except BudgetUnavailable:
                            break
                        except Exception as exc:
                            log.warning("outbound deferred: %s", type(exc).__name__)
                        finally:
                            with factory.begin() as session:
                                session.merge(State(key="outbound_cursor", value={"index": start + i + 1}))
        except TimeoutError:
            totals["state"] = "deadline_reached_partial"
        finally:
            await web.close()
    return totals


async def outbound_scan(factory, settings, company):
    preferences, web = load_preferences(settings), Web()
    try:
        with transaction_lock(factory, "outbound") as guard:
            if guard is None:
                return 0
            return await find_signals(factory, settings, web, preferences, company)
    finally:
        await web.close()
