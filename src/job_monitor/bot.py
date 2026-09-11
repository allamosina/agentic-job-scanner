import logging

from sqlalchemy import select
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from .applications import report_text, sync_notion
from .config import load_preferences, load_profile
from .delivery import build_queue, dispatch, slot_now, summary
from .models import Delivery, Evaluation, Feedback, Job, State, Version, utcnow
from .sources import Web
from .store import feedback_summary

log = logging.getLogger(__name__)
REASONS = {
    "salary": "Зарплата",
    "location": "География",
    "function": "Обязанности",
    "level": "Уровень",
    "company": "Компания",
    "contract": "Оформление",
}

BOT_COMMANDS = [
    ("jobs", "Получить новые подходящие вакансии сейчас"),
    ("scan", "Запустить поиск и прислать подборку"),
    ("status", "Технический статус"),
    ("saved", "Сохранённые вакансии"),
    ("preferences", "Правила поиска"),
    ("apps", "Отклики в Notion"),
    ("pause", "Приостановить рассылки"),
    ("resume", "Возобновить рассылки"),
]


def build_bot(factory, settings):
    if not all([settings.telegram_bot_token, settings.telegram_user_id, settings.telegram_chat_id]):
        raise ValueError("Set TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID, TELEGRAM_CHAT_ID in .env")

    def authorized(update):
        return (
            update.effective_user is not None
            and update.effective_chat is not None
            and update.effective_user.id == settings.telegram_user_id
            and update.effective_chat.id == settings.telegram_chat_id
        )

    async def start(update, context):
        if not authorized(update):
            return
        await update.message.reply_text(
            "Монитор вакансий. Подборки в 08:00, 13:00 и 18:00 по Праге.\n"
            "/jobs — прислать новые подходящие вакансии сейчас\n"
            "/scan — запустить поиск и прислать подборку (использует лимит API)\n"
            "/status — источники и очередь\n/preferences — правила и реакции\n/saved — сохранённые\n"
            "/pause и /resume — рассылки\n/reset_learning — сброс влияния реакций\n"
            "/apps — Notion: этапы откликов и результаты\n"
            "Комментарий к вакансии можно отправить ответом на её карточку."
        )

    async def deliver_now(update, context):
        prefs = load_preferences(settings)
        # A repeated Telegram update cannot create a second digest for the same request.
        slot = f"manual:{update.effective_chat.id}:{update.message.message_id}"
        build_queue(factory, settings, prefs, slot)
        web = Web()
        try:
            await dispatch(factory, context.bot, web, settings)
        finally:
            await web.close()

    async def manual_work(update, context, search):
        try:
            if search:
                from .worker import scan

                result = await scan(factory, settings)
                if result.get("state") == "already_running":
                    await update.message.reply_text("Поиск уже выполняется. Готовые вакансии доступны через /jobs.")
                    return
                notes = ["Поиск завершён." if result.get("state") != "deadline_reached_partial"
                         else "Поиск завершён частично: достигнут лимит времени."]
                if "evaluated" in result:
                    notes.append(f"Новых оценок: {result['evaluated']}.")
                if result.get("evaluation_state") == "disabled":
                    notes.append("AI-анализ выключен или не настроены ключ и модель.")
                if result.get("evaluation_state") == "waiting_for_notion":
                    notes.append("Анализ ждёт обновления откликов из Notion.")
                if result.get("evaluation_state") == "budget_exhausted":
                    notes.append("Дневной лимит запросов API исчерпан; оставшиеся вакансии ждут анализа.")
                if result.get("evaluation_errors"):
                    notes.append("Часть вакансий не удалось оценить из-за ошибок API.")
                await update.message.reply_text(" ".join(notes))
            await deliver_now(update, context)
        except Exception as exc:
            log.error("manual job request failed: %s", type(exc).__name__)
            await update.message.reply_text(
                "Не удалось завершить запрос. Результаты, уже сохранённые в базе, не потеряны. "
                "Причину нужно проверить в Deploy Logs."
            )
        finally:
            context.application.bot_data["manual_busy"] = False

    async def request_jobs(update, context):
        if not authorized(update):
            return
        if context.application.bot_data.get("manual_busy"):
            await update.message.reply_text("Предыдущий запрос ещё выполняется. Я пришлю результат сюда.")
            return
        with factory() as session:
            paused = session.get(State, "paused")
            if paused and paused.value.get("enabled"):
                await update.message.reply_text("Рассылки на паузе. Отправь /resume, затем повтори команду.")
                return
        search = update.message.text.split()[0].split("@")[0] == "/scan"
        context.application.bot_data["manual_busy"] = True
        try:
            await update.message.reply_text(
                "Запускаю поиск и анализ в пределах дневного лимита API. Это может занять несколько минут; "
                "результат пришлю сюда."
                if search else "Проверяю готовые вакансии и присылаю новые подходящие карточки."
            )
            context.application.create_task(manual_work(update, context, search), update=update)
        except Exception:
            context.application.bot_data["manual_busy"] = False
            raise

    async def status(update, context):
        if not authorized(update):
            return
        with factory() as session:
            report = summary(session)
            states = (
                session.execute(
                    select(Delivery.status).where(
                        Delivery.status.in_(["unknown", "sending", "verification_failed", "failed"])
                    )
                )
                .scalars()
                .all()
            )
        await update.message.reply_text(
            report + f"\nОтправок, требующих проверки: {len(states)}.\n"
            "Платные API: " + ("включены" if settings.paid_apis_enabled else "выключены")
        )

    async def applications(update, context):
        if not authorized(update):
            return
        with factory() as session:
            report = report_text(session, settings)
        await update.message.reply_text(report)

    async def notion_tick(context):
        try:
            await sync_notion(factory, settings)
        except Exception as exc:
            log.error("Notion sync failed: %s", type(exc).__name__)

    async def preferences(update, context):
        if not authorized(update):
            return
        prefs = load_preferences(settings)
        with factory() as session:
            ratings = feedback_summary(session, settings.telegram_user_id)
        await update.message.reply_text(
            f"Правила: приватная конфигурация, версия {prefs.version}.\n"
            f"Расписание: {', '.join(prefs.schedule.delivery_times)} {prefs.schedule.timezone}.\n"
            f"Основной профиль: {load_profile(settings).get('canonical_document', 'не задан')}.\n"
            f"Категории gross base CZK/месяц: HIGH ≥ {prefs.compensation_czk['high_min']}, "
            f"MEDIUM ≥ {prefs.compensation_czk['medium_min']}; ниже LOW. UNKNOWN допустим.\n"
            f"Активных оценок интереса: {len(ratings)}. Реакции меняют порядок внутри подходящих групп, "
            "но не факты CV и не обязательные ограничения."
        )

    async def toggle(update, context):
        if not authorized(update):
            return
        paused = update.message.text.split()[0].split("@")[0] == "/pause"
        with factory.begin() as session:
            session.merge(State(key="paused", value={"enabled": paused}))
        await update.message.reply_text("Рассылки приостановлены." if paused else "Рассылки возобновлены.")

    async def reset_learning(update, context):
        if not authorized(update):
            return
        with factory.begin() as session:
            session.merge(State(key="learning_reset", value={"at": utcnow().isoformat()}))
        await update.message.reply_text("Влияние прежних реакций сброшено. История событий сохранена.")

    async def saved(update, context):
        if not authorized(update):
            return
        with factory() as session:
            rows = (
                session.scalars(
                    select(Job)
                    .join(Feedback)
                    .where(Feedback.user_id == str(settings.telegram_user_id), Feedback.action == "save")
                    .order_by(Feedback.created_at.desc())
                    .limit(20)
                )
                .unique()
                .all()
            )
            message = "\n\n".join(f"{j.company} — {j.title}\n{j.canonical_url}" for j in rows)
        await update.message.reply_text(message[:4000] or "Сохранённых вакансий пока нет.")

    async def callback(update, context):
        query = update.callback_query
        if not authorized(update):
            await query.answer()
            return
        parts = (query.data or "").split(":")
        if len(parts) != 2:
            await query.answer()
            return
        action, job_id = parts
        if action not in {"like", "dislike", "save", "applied", "why", *REASONS.keys()}:
            await query.answer()
            return
        with factory.begin() as session:
            job = session.get(Job, job_id)
            delivery = session.scalar(
                select(Delivery)
                .join(Version, Version.id == Delivery.version_id)
                .where(
                    Version.job_id == job_id,
                    Delivery.chat_id == str(settings.telegram_chat_id),
                    Delivery.message_id == query.message.message_id,
                )
            )
            if not job or not delivery:
                await query.answer("Карточка не найдена.")
                return
            if action == "why":
                evaluation = session.get(Evaluation, delivery.evaluation_id)
                r = evaluation.result
                message = "\n".join(
                    [
                        "Сопоставление с CV:",
                        *[
                            f"• {d['responsibility']} → {d['match']} ({', '.join(d['evidence_ids']) or 'пробел'})"
                            for d in r["duties"]
                        ],
                        "Реалистичность перехода: " + r["career_transition_realism"],
                        "Компоненты оценки: " + str(r["points"]),
                        "Пробелы: " + "; ".join(r["gaps"]),
                    ]
                )
            else:
                key = "callback:" + query.id
                if not session.scalar(select(Feedback).where(Feedback.event_key == key)):
                    session.add(
                        Feedback(
                            event_key=key,
                            user_id=str(settings.telegram_user_id),
                            job_id=job_id,
                            action="reason" if action in REASONS else action,
                            reason=action if action in REASONS else None,
                        )
                    )
        await query.answer("Сохранено" if action != "why" else None)
        if action == "why":
            await query.message.reply_text(message[:4000])
        if action in REASONS:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(**button) for button in row] for row in delivery.buttons]
            )
            await query.edit_message_reply_markup(markup)
        if action == "dislike":
            markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(title, callback_data=f"{key}:{job_id}")]
                    for key, title in REASONS.items()
                ]
            )
            # Keep reason buttons on the original persisted card; comments can reply to it too.
            await query.edit_message_reply_markup(markup)

    async def comment(update, context):
        if not authorized(update) or not update.message.reply_to_message:
            return
        with factory.begin() as session:
            delivery = session.scalar(
                select(Delivery).where(
                    Delivery.chat_id == str(settings.telegram_chat_id),
                    Delivery.message_id == update.message.reply_to_message.message_id,
                )
            )
            if not delivery or not delivery.version_id:
                return
            key = f"message:{settings.telegram_chat_id}:{update.message.message_id}"
            if not session.scalar(select(Feedback).where(Feedback.event_key == key)):
                version = session.get(Version, delivery.version_id)
                session.add(
                    Feedback(
                        event_key=key,
                        user_id=str(settings.telegram_user_id),
                        job_id=version.job_id,
                        action="comment",
                        comment=update.message.text[:4000],
                    )
                )
        await update.message.reply_text(
            "Комментарий сохранён. Он не изменяет исходные правила автоматически."
        )

    async def tick(context):
        try:
            prefs = load_preferences(settings)
            slot = slot_now(utcnow(), prefs.schedule)
            if slot:
                build_queue(factory, settings, prefs, slot)
            with factory() as session:
                paused = session.get(State, "paused")
                if paused and paused.value.get("enabled"):
                    return
            await dispatch(factory, context.bot, context.application.bot_data["web"], settings)
        except Exception as exc:
            log.error("delivery tick failed: %s", type(exc).__name__)

    async def init(application):
        application.bot_data["web"] = Web()
        try:
            await application.bot.set_my_commands([BotCommand(name, text) for name, text in BOT_COMMANDS])
        except Exception as exc:
            log.warning("Telegram command menu unavailable: %s", type(exc).__name__)
        # Daily local-date gate is persisted in PostgreSQL; restarts do not re-import repeatedly.
        # The scan worker shares this lock/gate. Failed attempts retry at most every 30 min.
        application.job_queue.run_repeating(
            notion_tick, interval=1800, first=1, job_kwargs={"max_instances": 1, "coalesce": True}
        )
        application.job_queue.run_repeating(
            tick, interval=30, first=1, job_kwargs={"max_instances": 1, "coalesce": True}
        )

    async def shutdown(application):
        await application.bot_data["web"].close()

    async def on_error(update, context):
        log.error("telegram handler failed: %s", type(context.error).__name__)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(init)
        .post_shutdown(shutdown)
        .build()
    )
    for name, handler in [
        ("start", start),
        ("jobs", request_jobs),
        ("scan", request_jobs),
        ("status", status),
        ("preferences", preferences),
        ("pause", toggle),
        ("resume", toggle),
        ("saved", saved),
        ("reset_learning", reset_learning),
        ("apps", applications),
    ]:
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, comment))
    app.add_error_handler(on_error)
    return app
