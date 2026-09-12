"""One daily collection, resumable evaluation, one quiet digest in the bot service."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .db import transaction_lock
from .delivery import build_queue, dispatch
from .models import Delivery, State, utcnow
from .preferences import runtime_preferences
from .worker import collect_jobs, evaluate_queue


async def daily_cycle(factory, settings, bot, web, now=None):
    now = now or utcnow()
    local = now.astimezone(ZoneInfo('Europe/Prague'))
    if local.hour < 8:
        return
    key = 'daily_search:' + local.date().isoformat()
    with transaction_lock(factory, 'daily_search') as guard:
        if guard is None:
            return
        with factory() as session:
            paused = session.get(State, 'paused')
            record = session.get(State, key)
            state = dict(record.value) if record else {}
            if (paused and paused.value.get('enabled')) or state.get('done'):
                return
            if state.get('retry_after', '') > now.isoformat():
                return
        if not state.get('collected'):
            result = await collect_jobs(factory, settings)
            if result.get('state') == 'already_running':
                return
            state.update(collected=True, collection=result)
            with factory.begin() as session:
                session.merge(State(key=key, value=dict(state)))
        result = await evaluate_queue(factory, settings)
        if result.get('evaluation_state') == 'already_running':
            return
        state['evaluation'] = result
        problem = result.get('evaluation_state', 'complete')
        retry = problem in {'deadline_reached_partial', 'waiting_for_notion'} or result.get('remaining', 0) > 0
        if problem in {'budget_exhausted', 'disabled'}:
            retry = False  # Resume on the next daily run, not a tight retry loop.
        state['done'] = not retry
        state['retry_after'] = (now + timedelta(minutes=30)).isoformat()
        with factory.begin() as session:
            notice = {
                'budget_exhausted': 'Достигнут дневной лимит API. Часть вакансий ждёт оценки; продолжу завтра.',
                'disabled': 'Не удалось оценить вакансии: проверь ключ, модель и включение платных API.',
                'waiting_for_notion': 'Оценка задержана: не удалось обновить отклики из Notion. Повторю позже.',
                'deadline_reached_partial': 'Оценка заняла больше времени. Сохранённую очередь продолжу позже сегодня.',
            }.get(problem)
            if not notice and result.get('evaluation_errors'):
                notice = 'Часть вакансий не удалось оценить. Повторю позже; результаты сохранены.'
            # One actionable failure notice per day, not repeated technical status reports.
            if notice and not session.scalar(select(Delivery).where(
                Delivery.chat_id == str(settings.telegram_chat_id), Delivery.slot == key,
                Delivery.kind == 'notice')):
                session.add(Delivery(chat_id=str(settings.telegram_chat_id), slot=key,
                                     kind='notice', body=notice, buttons=[]))
        if state['done']:
            build_queue(factory, settings, runtime_preferences(factory, settings), key)
        with factory.begin() as session:
            session.merge(State(key=key, value=dict(state)))
        await dispatch(factory, bot, web, settings)
