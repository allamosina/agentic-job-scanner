import argparse
import asyncio
import json
import logging
import sys

from sqlalchemy import text

from .config import Settings, fingerprint, load_preferences, load_profile, load_sources


def main():
    parser = argparse.ArgumentParser(description="Personal vacancy monitor")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config")
    sub.add_parser("doctor")
    sub.add_parser("migrate")
    sub.add_parser("bot")
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("--force", action="store_true")
    scan_parser.add_argument(
        "--scheduled", action="store_true", help="Run only in the local 30-minute preparation window"
    )
    dry = sub.add_parser("probe", help="Read-only public source check; no DB, LLM or Telegram")
    dry.add_argument("source")
    out = sub.add_parser("outbound")
    out.add_argument("company")
    sub.add_parser("status")
    notion = sub.add_parser("notion-sync", help="Read-only daily Notion import")
    notion.add_argument("--force", action="store_true")
    sub.add_parser("applications", help="Application stages, matching diagnostics, outcome learning")
    link = sub.add_parser("link-application", help="Explicit local identity link; does not write to Notion")
    link.add_argument("page_id")
    link.add_argument("job_id")
    unlink = sub.add_parser("unlink-application", help="Remove an explicit local link")
    unlink.add_argument("page_id")
    unlink.add_argument("job_id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings.from_env()
    preferences = load_preferences(settings)
    sources = load_sources(settings)
    profile = load_profile(settings)
    from .applications import load_notion

    load_notion(settings)
    if args.command in {"bot", "scan", "outbound", "notion-sync"} and not settings.has_private_config:
        raise ValueError("Configure PRIVATE_CONFIG_JSON, its numbered parts, or PRIVATE_CONFIG_PATH")
    if args.command == "scan" and args.scheduled:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(preferences.schedule.timezone))
        due = False
        for slot in preferences.schedule.delivery_times:
            hour, minute = map(int, slot.split(":"))
            start = now.replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(minutes=30)
            due = due or 0 <= (now - start).total_seconds() < 600
        if not due:
            print("Outside the local preparation window; no scan.")
            return
    if args.command == "check-config":
        print(
            json.dumps(
                {
                    "valid": True,
                    "policy_hash": fingerprint(preferences),
                    "evidence_count": len(profile["evidence"]),
                    "sources": len(sources),
                    "schedule": preferences.schedule.model_dump(),
                    "paid_apis_enabled": settings.paid_apis_enabled,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.command == "probe":
        from .sources import Web, fetch_source

        source = next((s for s in sources if s.id == args.source), None)
        if source is None or source.kind == "search":
            parser.error("Choose a configured public source (no paid search for probe)")

        async def probe():
            web = Web()
            try:
                result = await fetch_source(web, source, preferences.operations.max_pages_per_source)
                print(
                    json.dumps(
                        dict(
                            source=source.id,
                            jobs=len(result.jobs),
                            complete=result.complete,
                            note=result.note,
                            examples=[{"title": j.title, "url": j.url} for j in result.jobs[:3]],
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            finally:
                await web.close()

        asyncio.run(probe())
        return
    if args.command == "doctor":
        present = {
            key: bool(getattr(settings, key))
            for key in [
                "private_config_json",
                "private_config_json_1",
                "private_config_json_2",
                "private_config_json_3",
                "private_config_json_4",
                "private_config_path",
                "database_url",
                "telegram_bot_token",
                "telegram_user_id",
                "telegram_chat_id",
                "openai_api_key",
                "openai_model",
                "brave_api_key",
                "notion_api_key",
            ]
        }
        # Only report presence, never values or exception text (may contain secrets).
        print(
            json.dumps(
                {
                    "configured": present,
                    "paid_apis_enabled": settings.paid_apis_enabled,
                    "notion_sync_enabled": settings.notion_sync_enabled,
                    "llm_daily_cap": settings.llm_calls_per_day,
                    "search_daily_cap": settings.search_calls_per_day,
                },
                indent=2,
            )
        )
        if not settings.database_url:
            print("PostgreSQL not configured. Fill .env and run migrate.")
            return
    if args.command == "migrate":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        print("PostgreSQL schema is current. No initial job/feedback history was inserted.")
        return
    from .db import database, sessions

    engine = database(settings)
    factory = sessions(engine)
    try:
        if args.command == "doctor":
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            print("PostgreSQL reachable")
        elif args.command == "scan":
            from .worker import scan

            print(json.dumps(asyncio.run(scan(factory, settings, args.force)), ensure_ascii=False))
        elif args.command == "outbound":
            from .worker import outbound_scan

            print("Signals saved:", asyncio.run(outbound_scan(factory, settings, args.company)))
        elif args.command == "status":
            from .delivery import summary

            with factory() as session:
                print(summary(session))
        elif args.command == "bot":
            from .bot import build_bot

            build_bot(factory, settings).run_polling(drop_pending_updates=False)
        elif args.command == "notion-sync":
            from .applications import sync_notion

            result = asyncio.run(sync_notion(factory, settings, args.force))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["status"] in {"failed", "disabled"}:
                raise ValueError("Notion sync did not run successfully")
        elif args.command == "applications":
            from .applications import outcome_report

            with factory() as session:
                print(json.dumps(outcome_report(session, settings), ensure_ascii=False, indent=2))
        elif args.command in {"link-application", "unlink-application"}:
            from uuid import UUID

            from sqlalchemy import delete

            from .applications import link_application
            from .models import ApplicationLink

            with factory.begin() as session:
                if args.command == "link-application":
                    link_application(session, args.page_id, args.job_id)
                else:
                    session.execute(
                        delete(ApplicationLink).where(
                            ApplicationLink.page_id == str(UUID(args.page_id)),
                            ApplicationLink.job_id == args.job_id,
                        )
                    )
            print("Local application link updated. Notion was not modified.")
    finally:
        engine.dispose()


def run():
    try:
        main()
    except Exception as exc:
        print(
            f"Command failed ({type(exc).__name__}). Check configuration and service status; secrets omitted.",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    run()
