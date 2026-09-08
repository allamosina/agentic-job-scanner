"""Notion is a read-only source of application facts, never executable instructions."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field, field_validator
from sqlalchemy import select

from .config import Strict, config_section, fingerprint, load_preferences
from .db import transaction_lock
from .models import (
    ApplicationEvent,
    ApplicationLink,
    Evaluation,
    Feedback,
    Job,
    NotionApplication,
    Observation,
    State,
    Version,
    utcnow,
)
from .sources import canonical_url


class Stage(Strict):
    applied: bool
    outcome: Literal["not_started", "pending", "offered", "rejected"]
    stage: int | None = Field(default=None, ge=0, le=5)


class NotionConfig(Strict):
    data_source_id: str
    database_url: str
    api_version: str = "2026-03-11"
    properties: dict[str, str]
    statuses: dict[str, Stage]
    max_stale_hours: int = Field(default=36, ge=24, le=168)
    retry_minutes: int = Field(default=30, ge=5, le=1440)
    learning_min_resolved: int = Field(default=5, ge=3)
    learning_max_adjustment: float = Field(default=2, ge=0, le=5)
    track_overrides: dict[str, str] = Field(default_factory=dict)

    @field_validator("data_source_id")
    @classmethod
    def valid_id(cls, value):
        return str(UUID(value))

    @field_validator("properties")
    @classmethod
    def required_properties(cls, value):
        if set(value) != {"company", "description", "status", "date", "channel", "comment"}:
            raise ValueError("Notion property mapping is incomplete")
        return value

    @field_validator("track_overrides")
    @classmethod
    def valid_tracks(cls, value):
        from typing import get_args

        from .evaluation import Assessment

        allowed = get_args(Assessment.model_fields["track"].annotation)
        if any(track not in allowed for track in value.values()):
            raise ValueError("Use an existing assessment track")
        return {str(UUID(key)): track for key, track in value.items()}


def load_notion(settings):
    return NotionConfig.model_validate(config_section(settings, "notion", settings.notion_config_path))


def property_value(prop):
    kind = prop["type"]
    value = prop.get(kind)
    if kind in {"title", "rich_text"}:
        # Never silently import truncated properties.
        if len(value or []) >= 25:
            raise ValueError("Long Notion property needs separate pagination")
        return "".join(v.get("plain_text", v.get("text", {}).get("content", "")) for v in value or [])
    if kind in {"status", "select"}:
        return value.get("name", "") if value else ""
    if kind == "date":
        return value.get("start") if value else None
    if kind == "url":
        return value or ""
    raise ValueError("Unsupported Notion property type")


def url_key(value):
    """Only actual vacancy identities; never company-domain matching."""
    if not isinstance(value, str):
        return None
    try:
        u = urlsplit(value.strip())
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
            return None
        host = u.hostname.lower()
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            match = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)/?$", u.path)
            return "linkedin:" + match[1] if match else None
        if host in {
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "boards.eu.greenhouse.io",
            "job-boards.eu.greenhouse.io",
        }:
            match = re.search(r"/([^/]+)/jobs/(\d+)/?$", u.path)
            if match:
                return "greenhouse:" + match[2]
        gh = parse_qs(u.query).get("gh_jid", [])
        if gh and re.fullmatch(r"\d+", gh[0]):
            return "greenhouse:" + gh[0]
        if host == "jobs.ashbyhq.com":
            match = re.match(r"/([^/]+)/([a-fA-F0-9-]{36})(?:/application)?/?$", u.path)
            if match:
                return f"ashby:{match[1].lower()}:{str(UUID(match[2]))}"
        if host.endswith(".teamtailor.com"):
            match = re.match(r"/jobs/(\d+)(?:-[^/]*)?/?$", u.path)
            if match:
                return f"teamtailor:{host}:{match[1]}"
        # Landing pages cannot establish vacancy identity.
        if u.path.rstrip("/").lower() in {"", "/jobs", "/careers", "/positions"}:
            return None
        return "url:" + canonical_url(value.strip())
    except (ValueError, TypeError):
        return None


def normalize_page(page, config):
    if page.get("object") != "page" or page.get("in_trash") or page.get("archived"):
        raise ValueError("Unexpected archived/non-page result")
    props = page["properties"]
    values = {key: property_value(props[name]) for key, name in config.properties.items()}
    rule = config.statuses.get(values["status"])
    return {
        "page_id": str(UUID(page["id"])),
        **values,
        "url_key": url_key(values["description"]),
        "applied": rule.applied if rule else None,
        "outcome": rule.outcome if rule else "unknown",
        "stage": rule.stage if rule else None,
    }


async def fetch_snapshot(client, config):
    schema_response = await client.get(f"/v1/data_sources/{config.data_source_id}")
    schema_response.raise_for_status()
    schema = schema_response.json()["properties"]
    expected_types = {
        "company": {"title"},
        "description": {"url", "rich_text"},
        "status": {"status", "select"},
        "date": {"date"},
        "channel": {"select"},
        "comment": {"rich_text"},
    }
    for key, name in config.properties.items():
        if schema[name]["type"] not in expected_types[key]:
            raise ValueError("Notion schema changed")
    pages, cursor, seen = {}, None, set()
    for _ in range(1000):
        response = await client.post(
            f"/v1/data_sources/{config.data_source_id}/query",
            json={"page_size": 100, **({"start_cursor": cursor} if cursor else {})},
        )
        response.raise_for_status()
        data = response.json()
        for page in data["results"]:
            row = normalize_page(page, config)
            if row["page_id"] in pages:
                raise ValueError("Unstable Notion pagination; retry full snapshot")
            pages[row["page_id"]] = row
        if data["has_more"] is False:
            return list(pages.values())
        cursor = data["next_cursor"]
        if not cursor or cursor in seen:
            raise ValueError("Invalid Notion pagination")
        seen.add(cursor)
    raise ValueError("Notion snapshot limit exceeded")


def apply_snapshot(session, rows, config, now):
    existing = {a.page_id: a for a in session.scalars(select(NotionApplication))}
    ids, changes = set(), 0
    for row in rows:
        key = row["page_id"]
        ids.add(key)
        app = existing.get(key)
        digest = fingerprint(row)
        if app is None:
            app = NotionApplication(
                page_id=key,
                data_source_id=config.data_source_id,
                snapshot=row,
                snapshot_hash=digest,
                first_seen=now,
                last_seen=now,
                max_observed_stage=row["stage"],
                missing=False,
            )
            session.add(app)
            session.flush()
            changed = True
        else:
            changed = app.snapshot_hash != digest or app.missing
            old_identity = (
                app.snapshot["company"],
                app.snapshot.get("url_key") or app.snapshot["description"],
            )
            new_identity = (row["company"], row.get("url_key") or row["description"])
            if old_identity != new_identity:
                # Reusing a Notion page for another role must not transfer its interview history.
                app.max_observed_stage = None
            app.snapshot, app.snapshot_hash, app.last_seen, app.missing = row, digest, now, False
            stages = [v for v in [app.max_observed_stage, row["stage"]] if v is not None]
            app.max_observed_stage = max(stages) if stages else None
        if changed:
            session.add(ApplicationEvent(page_id=key, snapshot=row, observed_at=now))
            changes += 1
    for key, app in existing.items():
        if app.data_source_id == config.data_source_id and key not in ids and not app.missing:
            app.missing = True
            session.add(ApplicationEvent(page_id=key, snapshot={"missing": True}, observed_at=now))
    return {
        "rows": len(rows),
        "changes": changes,
        "unknown_statuses": sum(r["applied"] is None for r in rows),
        "no_job_url": sum(r["url_key"] is None for r in rows),
    }


async def sync_notion(factory, settings, force=False, client=None, now=None):
    if not settings.notion_sync_enabled:
        return {"status": "disabled"}
    config, now = load_notion(settings), now or utcnow()
    zone = ZoneInfo(load_preferences(settings).schedule.timezone)
    with transaction_lock(factory, "notion-sync") as session:
        if session is None:
            return {"status": "already_running"}
        state = session.get(State, "notion_sync")
        previous = state.value if state else {}
        same_source = previous.get("data_source_id") == config.data_source_id
        success = previous.get("last_success") if same_source else None
        same_config = previous.get("success_config_hash") == fingerprint(config)
        if (
            not force
            and success
            and same_config
            and previous.get("status") == "complete"
            and datetime.fromisoformat(success).astimezone(zone).date() == now.astimezone(zone).date()
        ):
            return {**previous, "status": "current_today"}
        attempted = (
            previous.get("last_attempt")
            if same_source and previous.get("config_hash") == fingerprint(config)
            else None
        )
        if (
            not force
            and attempted
            and datetime.fromisoformat(attempted) > now - timedelta(minutes=config.retry_minutes)
        ):
            return previous
        state_value = {
            **previous,
            "data_source_id": config.data_source_id,
            "config_hash": fingerprint(config),
            "last_attempt": now.isoformat(),
            "last_success": success,
        }
        try:
            if not settings.notion_api_key:
                raise ValueError("Notion key missing")

            async def fetch(c):
                import asyncio

                async with asyncio.timeout(120):
                    return await fetch_snapshot(c, config)

            if client is not None:
                rows = await fetch(client)
            else:
                async with httpx.AsyncClient(
                    base_url="https://api.notion.com",
                    timeout=30,
                    follow_redirects=False,
                    headers={
                        "Authorization": "Bearer " + settings.notion_api_key,
                        "Notion-Version": config.api_version,
                    },
                ) as c:
                    rows = await fetch(c)
            # Apply only a complete snapshot. A DB failure rolls back both rows and success marker.
        except (httpx.HTTPError, ValueError, KeyError, TypeError, TimeoutError) as exc:
            state_value.update(status="failed", error=type(exc).__name__)
        else:
            counts = apply_snapshot(session, rows, config, now)
            state_value.update(
                status="complete",
                error=None,
                last_success=now.isoformat(),
                success_config_hash=fingerprint(config),
                **counts,
            )
        session.merge(State(key="notion_sync", value=state_value))
        return state_value


def freshness_problem(session, settings):
    if not settings.notion_sync_enabled:
        return None
    config = load_notion(settings)
    state = session.get(State, "notion_sync")
    value = state.value if state else {}
    at = value.get("last_success")
    if value.get("data_source_id") != config.data_source_id or not at:
        return "Notion: первая синхронизация не завершена; карточки временно отложены."
    if value.get("success_config_hash") != fingerprint(config):
        return "Notion: конфиг изменён; карточки отложены до новой синхронизации."
    if datetime.fromisoformat(at) < utcnow() - timedelta(hours=config.max_stale_hours):
        return "Notion: данные устарели; карточки отложены до успешной синхронизации."
    return None


def matching_index(session):
    jobs = {j.id: j for j in session.scalars(select(Job))}
    keys = defaultdict(set)
    for job in jobs.values():
        for url in (job.canonical_url, job.official_url):
            if key := url_key(url):
                keys[key].add(job.id)
    primary_keys = set(keys)
    for obs in session.scalars(select(Observation)):
        if key := url_key(obs.url):
            if key not in primary_keys:
                keys[key].add(obs.job_id)
    links = defaultdict(set)
    for link in session.scalars(select(ApplicationLink)):
        links[link.page_id].add(link.job_id)
    apps = session.scalars(select(NotionApplication)).all()
    matches = {}
    conflicts = set()
    companies = defaultdict(set)

    def company_key(value):
        return "".join(c for c in value.casefold() if c.isalnum())

    for app in apps:
        if key := app.snapshot.get("url_key"):
            companies[key].add(company_key(app.snapshot["company"]))
    for app in apps:
        key = app.snapshot.get("url_key")
        matched = keys.get(key, set()) if key else set()
        # Greenhouse/custom-page IDs and copied URLs need company agreement.
        matched = {j for j in matched if company_key(jobs[j].company) == company_key(app.snapshot["company"])}
        if (key and len(companies[key]) > 1) or len(matched) > 1:
            conflicts.add(app.page_id)
            matched = set()
        matches[app.page_id] = matched | links[app.page_id]
    return apps, matches, conflicts


def blocked_job_ids(session, user_id):
    blocked = set(
        session.scalars(
            select(Feedback.job_id).where(Feedback.user_id == str(user_id), Feedback.action == "applied")
        )
    )
    apps, matches, _ = matching_index(session)
    for app in apps:
        # Unknown new statuses conservatively hold only exact matches for review.
        if app.snapshot["applied"] is not False:
            blocked.update(matches[app.page_id])
    return blocked


def outcome_report(session, settings):
    config = load_notion(settings)
    apps, matches, conflicts = matching_index(session)
    evals = session.execute(
        select(Version.job_id, Evaluation.result)
        .join(Evaluation, Evaluation.version_id == Version.id)
        .order_by(Evaluation.created_at)
    ).all()
    tracks = {job_id: result["track"] for job_id, result in evals}
    groups, details = {}, []
    duplicate_counts = Counter(
        (a.snapshot["company"].casefold(), a.snapshot.get("url_key") or a.page_id) for a in apps
    )
    for app in sorted(apps, key=lambda a: a.page_id):
        row = app.snapshot
        candidates = {tracks[j] for j in matches[app.page_id] if j in tracks}
        track = config.track_overrides.get(app.page_id) or (
            next(iter(candidates)) if len(candidates) == 1 else "UNCLASSIFIED"
        )
        key = (row["company"].casefold(), row.get("url_key") or app.page_id)
        duplicate = duplicate_counts[key] > 1
        details.append(
            {
                "page_id": app.page_id,
                "company": row["company"],
                "status": row["status"],
                "track": track,
                "matched_jobs": sorted(matches[app.page_id]),
                "max_observed_stage": app.max_observed_stage,
                "missing": app.missing,
                "conflicting_url": app.page_id in conflicts,
                "duplicate_url": duplicate,
                "description": row["description"],
            }
        )
        if row["applied"] is not True or duplicate or app.page_id in conflicts or app.missing:
            continue
        group = groups.setdefault(
            (track, row["channel"] or "Unknown"),
            {
                "applied": 0,
                "progressed": 0,
                "progress_points": 0,
                "offered": 0,
                "rejected": 0,
                "rejected_stage_unknown": 0,
                "pending": 0,
            },
        )
        group["applied"] += 1
        group["progressed"] += int((app.max_observed_stage or 0) >= 1)
        group["progress_points"] += app.max_observed_stage or 0
        group[row["outcome"]] += 1
        # Observing Applied then Rejected doesn't prove interviews never happened between polls.
        group["rejected_stage_unknown"] += int(row["outcome"] == "rejected" and not app.max_observed_stage)
    # Weak, channel-controlled empirical OFFER conversion, not causal fit or pre-HR rejection.
    # Pending applications do not enter denominators. Small groups have zero influence.
    adjustments = defaultdict(list)
    progress_adjustments = defaultdict(list)
    for (track, channel), group in groups.items():
        if track != "UNCLASSIFIED" and group["progressed"] >= config.learning_min_resolved:
            # Positive evidence only: unobserved progress/pending applications are not failures.
            progress_adjustments[track].append(
                config.learning_max_adjustment
                * 0.25
                * group["progress_points"]
                / (5 * (group["progressed"] + config.learning_min_resolved))
            )
        n = group["offered"] + group["rejected"]
        others = [g for (t, c), g in groups.items() if c == channel and t != track and t != "UNCLASSIFIED"]
        other_n = sum(g["offered"] + g["rejected"] for g in others)
        if (
            track == "UNCLASSIFIED"
            or n < config.learning_min_resolved
            or other_n < config.learning_min_resolved
        ):
            continue
        baseline = (sum(g["offered"] for g in others) + 1) / (other_n + 2)
        estimate = (group["offered"] + config.learning_min_resolved * baseline) / (
            n + config.learning_min_resolved
        )
        adjustments[track].append(config.learning_max_adjustment * (estimate - baseline))
    state = session.get(State, "notion_sync")
    return {
        "sync": state.value if state else {"status": "not_synced"},
        "enabled": settings.notion_sync_enabled,
        "statuses": dict(Counter(a.snapshot["status"] for a in apps)),
        "rows": len(apps),
        "unmatched_applied": sum(a.snapshot["applied"] is True and not matches[a.page_id] for a in apps),
        "groups": [{"track": t, "channel": c, **g} for (t, c), g in groups.items()],
        "adjustments": {
            t: max(
                -config.learning_max_adjustment,
                min(
                    config.learning_max_adjustment,
                    (sum(adjustments[t]) / len(adjustments[t]) if adjustments[t] else 0)
                    + (
                        sum(progress_adjustments[t]) / len(progress_adjustments[t])
                        if progress_adjustments[t]
                        else 0
                    ),
                ),
            )
            for t in set(adjustments) | set(progress_adjustments)
        },
        "applications": details,
    }


def report_text(session, settings):
    report = outcome_report(session, settings)
    lines = [
        f"Notion: {report['sync'].get('status')}; включён: {report['enabled']}. "
        f"Последний успех: {report['sync'].get('last_success') or 'ещё нет'}.",
        f"Записей: {report['rows']}. Статусы: {report['statuses']}.",
        f"Откликов без точной связи с вакансией: {report['unmatched_applied']}.",
    ]
    for g in report["groups"]:
        lines.append(
            f"{g['track']} / {g['channel']}: откликов {g['applied']}, дошла до HR+ {g['progressed']}, "
            f"offer {g['offered']}, reject {g['rejected']} (этап неизвестен {g['rejected_stage_unknown']}), "
            f"ожидают {g['pending']}."
        )
    lines += [
        f"Поправки к порядку: {report['adjustments'] or 'недостаточно сопоставимых данных'}.",
        "Это наблюдения, не причины успеха/отказа. Pending не считается неудачей. "
        "Пропущенные между проверками этапы неизвестны. Неопознанные роли не обучают ранжирование.",
    ]
    return "\n".join(lines)[:3950]


def link_application(session, page_id, job_id):
    page_id = str(UUID(page_id))
    if not session.get(NotionApplication, page_id) or not session.get(Job, job_id):
        raise ValueError("Application or job not found")
    if not session.scalar(
        select(ApplicationLink).where(ApplicationLink.page_id == page_id, ApplicationLink.job_id == job_id)
    ):
        session.add(ApplicationLink(page_id=page_id, job_id=job_id))
