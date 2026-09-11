from __future__ import annotations

import difflib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select

from .applications import blocked_job_ids, freshness_problem, outcome_report
from .config import fingerprint, load_profile
from .db import transaction_lock
from .models import Delivery, Evaluation, Job, Run, Signal, State, Version, utcnow
from .store import feedback_boost


def slot_now(now, schedule):
    local = now.astimezone(ZoneInfo(schedule.timezone))
    if local.strftime("%H:%M") not in schedule.delivery_times:
        return None
    return local.strftime("%Y-%m-%dT%H:%M")


def material_change(previous, current):
    for key in ("location", "remote_policy", "employment_type", "salary"):
        if previous.get(key) != current.get(key):
            return True
    if previous.get("posted_at") and current.get("posted_at") != previous.get("posted_at"):
        return True
    # Exact policy-related text changes require re-review even if the JD is long.
    import re

    policy = re.compile(
        r"[^.!?]*(?:czech|czechia|payroll|employer of record|\beor\b|remote policy)[^.!?]*", re.IGNORECASE
    )
    old, new = previous.get("description", ""), current.get("description", "")
    if policy.findall(old) != policy.findall(new):
        return True
    return difflib.SequenceMatcher(None, old, new, autojunk=False).ratio() < 0.95


def card(job, version, evaluation):
    r, j = evaluation.result, version.payload
    parts = [
        f"{job.company} — {job.title}",
        f"{evaluation.score}/100 · {evaluation.category} · {r['track']}",
        f"Локация: {job.location} | {j.get('remote_policy', 'Unknown')}",
        f"Опубликовано: {j.get('posted_at') or 'неизвестно'}",
        f"Найм из Чехии: {r['employment_feasibility']} · {r['employment_model']}",
        f"Компенсация: {r['compensation_fit']} — {r['compensation_summary']}",
        "Почему подходит:",
    ]
    parts += ["• " + x for x in r["why_it_fits"][:4]]
    parts += ["Риски: " + "; ".join(r["gaps"]), "Действие: " + r["recommended_action"]]
    if r.get("pmm"):
        p = r["pmm"]
        parts += [
            "Переход в PMM: " + p["why_pmm_transition_is_credible"],
            "Обязательный PMM-опыт: " + ("да" if p["hard_pmm_experience_required"] else "нет"),
            "Потенциал кейса: " + p["case_study_potential"],
            "Идея кейса: " + p["suggested_case_angle"],
        ]
    parts += [
        "Источник: " + j["source_id"],
        "Найдено: " + j["discovered_url"],
        "Официальная вакансия: " + (job.official_url or "не подтверждена"),
        job.canonical_url,
    ]
    # Buttons preserve links if unusually long text needs truncation.
    return "\n".join(parts)[:3800]


def summary(session):
    jobs = session.scalar(select(func.count()).select_from(Job))
    evaluated = session.scalar(select(func.count()).select_from(Evaluation))
    runs = session.scalars(select(Run).order_by(Run.started_at.desc()).limit(500)).all()
    latest = {}
    for run in runs:
        latest.setdefault(run.source_id, run)
    statuses = {}
    for run in latest.values():
        statuses[run.status] = statuses.get(run.status, 0) + 1
    planned = session.get(State, "coverage_plan")
    raw = sum(r.counts.get("raw", 0) for r in latest.values())
    duplicates = sum(r.counts.get("duplicates", 0) for r in latest.values())
    rejected = {}
    for result in session.scalars(
        select(Evaluation.result).order_by(Evaluation.created_at.desc()).limit(500)
    ):
        if result.get("reasons"):
            reason = result["reasons"][0].split(":", 1)[0]
            rejected[reason] = rejected.get(reason, 0) + 1
    return (
        f"В базе вакансий: {jobs}. Оценок: {evaluated}.\n"
        f"Источников в плане: {planned.value['enabled_sources'] if planned else 'не сформирован'}. "
        f"Последние попытки: {statuses or 'ещё не было'}.\n"
        f"Raw в последних обходах: {raw}; повторов: {duplicates}.\n"
        f"Основные причины отказа (последние 500 оценок): {rejected or 'нет данных'}."
    )


def manual_digest_text(count, notion_problem):
    if notion_problem:
        return "Подборка задержана: нужно обновить отклики из Notion, чтобы не предложить уже поданные вакансии."
    if count:
        return (
            f"Выбрано новых вакансий: {count}. Карточки отправляются после проверки актуальности. "
            "На каждой можно отметить интерес, сохранить её или указать, что уже откликнулась."
        )
    return (
        "Новых готовых рекомендаций сейчас нет: подходящие карточки ещё не подготовлены "
        "или уже были отправлены. Это не означает, что все собранные вакансии проверены. "
        "Отправь /scan, чтобы запустить поиск и анализ в пределах дневного лимита API."
    )


def build_queue(factory, settings, preferences, slot):
    with transaction_lock(factory, "digest:" + str(settings.telegram_chat_id)) as session:
        if session is None:
            return
        pause = session.get(State, "paused")
        if pause and pause.value.get("enabled"):
            return
        chat = str(settings.telegram_chat_id)
        if session.scalar(
            select(Delivery).where(
                Delivery.chat_id == chat, Delivery.slot == slot, Delivery.kind == "summary"
            )
        ):
            return
        profile_hash, policy_hash = fingerprint(load_profile(settings)), fingerprint(preferences)
        latest = (
            select(Evaluation.version_id, func.max(Evaluation.created_at).label("latest"))
            .where(
                Evaluation.profile_hash == profile_hash,
                Evaluation.policy_hash == policy_hash,
                Evaluation.model == settings.openai_model,
            )
            .group_by(Evaluation.version_id)
            .subquery()
        )
        rows = session.execute(
            select(Job, Version, Evaluation)
            .join(Version, Version.job_id == Job.id)
            .join(Evaluation, Evaluation.version_id == Version.id)
            .join(
                latest,
                and_(Evaluation.version_id == latest.c.version_id, Evaluation.created_at == latest.c.latest),
            )
            .where(
                Job.active.is_(True),
                Version.content_hash == Job.current_hash,
                Evaluation.eligible.is_(True),
                Evaluation.profile_hash == profile_hash,
                Evaluation.policy_hash == policy_hash,
                Evaluation.model == settings.openai_model,
            )
        ).all()
        all_evaluations = session.execute(
            select(Version.job_id, Evaluation.result)
            .join(Evaluation, Evaluation.version_id == Version.id)
            .order_by(Evaluation.created_at)
        ).all()
        tracks = {job_id: result["track"] for job_id, result in all_evaluations}
        boosts = {
            track: feedback_boost(session, settings.telegram_user_id, track, tracks)
            for track in set(tracks.values())
        }
        blocked = blocked_job_ids(session, settings.telegram_user_id)
        notion_problem = freshness_problem(session, settings)
        outcome_boosts = outcome_report(session, settings)["adjustments"]
        employment_order = {"employee": 3, "eor": 2, "unknown": 1, "contractor": 0}
        rows.sort(
            key=lambda x: (
                x[2].score // 10,
                employment_order[x[2].result["employment_model"]],
                x[2].score
                + boosts.get(x[2].result["track"], 0)
                + outcome_boosts.get(x[2].result["track"], 0),
            ),
            reverse=True,
        )
        count = 0
        for job, version, evaluation in rows:
            if job.id in blocked or notion_problem:
                continue
            previous = session.execute(
                select(Delivery, Version)
                .join(Version, Delivery.version_id == Version.id)
                .where(
                    Delivery.chat_id == chat,
                    Version.job_id == job.id,
                    Delivery.status.in_(["pending", "sending", "sent", "unknown"]),
                )
                .order_by(Delivery.created_at.desc())
                .limit(1)
            ).first()
            if previous and (
                previous[1].id == version.id or not material_change(previous[1].payload, version.payload)
            ):
                continue
            if session.scalar(
                select(Delivery).where(Delivery.chat_id == chat, Delivery.version_id == version.id)
            ):
                continue
            kind = "job:" + job.id
            body = card(job, version, evaluation)
            if previous:
                body = "Обновлённая вакансия\n" + body
            buttons = [
                [
                    {"text": "Интересно", "callback_data": "like:" + job.id},
                    {"text": "Не подходит", "callback_data": "dislike:" + job.id},
                ],
                [
                    {"text": "Сохранить", "callback_data": "save:" + job.id},
                    {"text": "Откликнулась", "callback_data": "applied:" + job.id},
                ],
                [
                    {"text": "Почему показано", "callback_data": "why:" + job.id},
                    {"text": "Открыть", "url": job.canonical_url},
                ],
            ]
            session.add(
                Delivery(
                    chat_id=chat,
                    slot=slot,
                    kind=kind,
                    version_id=version.id,
                    evaluation_id=evaluation.id,
                    body=body,
                    buttons=buttons,
                )
            )
            count += 1
            if count >= preferences.operations.max_cards_per_slot:
                break
        # Outbound results created by explicit outbound scans join the same delivery windows.
        for signal in session.scalars(select(Signal).order_by(Signal.created_at).limit(100)):
            key = "signal:" + signal.id
            if session.get(State, key):
                continue
            r = signal.result
            body = "\n".join(
                [
                    "OUTBOUND · " + signal.company,
                    r["signal"],
                    "Дата: " + r["event_date"],
                    "Почему сейчас: " + r["why_now"],
                    "Функция: " + r["likely_function"],
                    "Контакт: " + r["contact_person_or_title"],
                    "Угол обращения: " + r["outreach_angle"],
                    *[p["url"] for p in r["proof"]],
                ]
            )[:3800]
            session.add(Delivery(chat_id=chat, slot=slot, kind=key, body=body, buttons=[]))
            session.add(State(key=key, value={"queued": True}))
        session.add(
            Delivery(
                chat_id=chat,
                slot=slot,
                kind="summary",
                buttons=[],
                # Keep the slot marker for idempotency, but scheduled reports are never sent.
                status="pending" if slot.startswith("manual:") else "suppressed",
                body=manual_digest_text(count, notion_problem) if slot.startswith("manual:") else "",
            )
        )


async def dispatch(factory, bot, web, settings=None):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

    from .config import Source, load_sources
    from .sources import canonical_url, fetch_source, jsonld_jobs

    configured = {s.id: s for s in load_sources(settings)} if settings else {}
    verified_boards = {}
    with factory.begin() as session:
        # A process may have crashed after Telegram accepted a message. Never blindly retry that claim.
        for stale in session.scalars(
            select(Delivery).where(
                Delivery.status == "sending", Delivery.attempted_at < utcnow() - timedelta(minutes=5)
            )
        ):
            stale.status = "unknown"

    # Serial sends, persistent claim before networking; timeout is ambiguous and never blindly retried.
    while True:
        with factory.begin() as session:
            delivery = session.scalar(
                select(Delivery)
                .where(
                    Delivery.status == "pending",
                    or_(Delivery.next_attempt_at.is_(None), Delivery.next_attempt_at <= utcnow()),
                )
                .order_by(Delivery.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if delivery is None:
                return
            # Also suppress reports queued by versions deployed before this change.
            if delivery.kind == "summary" and not delivery.slot.startswith("manual:"):
                delivery.status = "suppressed"
                continue
            if delivery.version_id:
                version = session.get(Version, delivery.version_id)
                job = session.get(Job, version.job_id)
                if settings and job.id in blocked_job_ids(session, settings.telegram_user_id):
                    delivery.status = "already_applied"
                    continue
                if settings and freshness_problem(session, settings):
                    delivery.next_attempt_at = utcnow() + timedelta(minutes=30)
                    continue
                url = job.official_url or job.canonical_url
                payload = version.payload
                if not job.active or version.content_hash != job.current_hash:
                    delivery.status = "superseded"
                    continue
            else:
                url = None
            delivery.status, delivery.attempted_at = "sending", utcnow()
            delivery.verification_attempts += int(bool(url))
            ident, chat, body, buttons = delivery.id, delivery.chat_id, delivery.body, delivery.buttons
        if url:
            try:
                source = configured.get(payload["source_id"])
                if source and source.kind in {"greenhouse", "lever", "ashby"}:
                    if source.id not in verified_boards:
                        verified_boards[source.id] = await fetch_source(web, source, 10)
                    fresh = verified_boards[source.id]
                    match = next((j for j in fresh.jobs if j.ats_id == payload.get("ats_id")), None)
                    if not match and fresh.complete:
                        raise ValueError("expired")
                    if match and material_change(payload, match.model_dump()):
                        from .store import save_job

                        with factory.begin() as session:
                            save_job(session, match)
                            session.get(Delivery, ident).status = "superseded"
                        continue
                page = await web.get(url)
                # JSON-LD can explicitly mark expiration. Lack of it isn't proof of closure.
                jobs = jsonld_jobs(page.text, str(page.url), Source(id="verify", kind="jsonld", url=url))
                for j in jobs:
                    if j.valid_through and canonical_url(j.url) == canonical_url(url):
                        expiry = datetime.fromisoformat(j.valid_through.replace("Z", "+00:00"))
                        if expiry.tzinfo is None:
                            expiry = expiry.replace(tzinfo=UTC)
                        if expiry < utcnow():
                            raise ValueError("expired")
            except Exception as exc:
                import httpx

                closed = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {404, 410}
                closed = closed or (isinstance(exc, ValueError) and str(exc) == "expired")
                with factory.begin() as session:
                    item = session.get(Delivery, ident)
                    item.status = (
                        "closed"
                        if closed
                        else ("pending" if item.verification_attempts < 3 else "verification_failed")
                    )
                    if not closed:
                        item.next_attempt_at = utcnow() + timedelta(minutes=15)
                    if closed:
                        job = session.get(Job, session.get(Version, item.version_id).job_id)
                        job.active = False
                continue
        try:
            # Notion/local feedback can change while a vacancy URL is being verified.
            if url and settings:
                with factory.begin() as session:
                    item = session.get(Delivery, ident)
                    job_id = session.get(Version, item.version_id).job_id
                    if job_id in blocked_job_ids(session, settings.telegram_user_id):
                        item.status = "already_applied"
                        continue
                    if freshness_problem(session, settings):
                        item.status = "pending"
                        item.next_attempt_at = utcnow() + timedelta(minutes=30)
                        continue
            markup = (
                InlineKeyboardMarkup([[InlineKeyboardButton(**b) for b in row] for row in buttons])
                if buttons
                else None
            )
            message = await bot.send_message(
                chat_id=int(chat), text=body, reply_markup=markup, disable_web_page_preview=True
            )
            with factory.begin() as session:
                item = session.get(Delivery, ident)
                item.status, item.message_id = "sent", message.message_id
        except RetryAfter as exc:
            with factory.begin() as session:
                item = session.get(Delivery, ident)
                item.status = "pending"
                wait = (
                    exc.retry_after
                    if isinstance(exc.retry_after, timedelta)
                    else timedelta(seconds=exc.retry_after)
                )
                item.next_attempt_at = utcnow() + wait
            return
        except (BadRequest, Forbidden):
            with factory.begin() as session:
                session.get(Delivery, ident).status = "failed"
        except NetworkError:
            with factory.begin() as session:
                session.get(Delivery, ident).status = "unknown"
        except Exception:
            with factory.begin() as session:
                session.get(Delivery, ident).status = "unknown"
            raise
