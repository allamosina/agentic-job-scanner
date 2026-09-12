from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from job_monitor import bot, scheduler, worker
from job_monitor.config import Settings, fingerprint, load_preferences
from job_monitor.models import Base, Delivery, Feedback, Job, State, Version
from job_monitor.preferences import runtime_preferences


@pytest.fixture
def db(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def lock(factory, key):
        with factory.begin() as session:
            yield session

    monkeypatch.setattr(scheduler, 'transaction_lock', lock)
    yield factory
    engine.dispose()


def handlers(db):
    settings = Settings(telegram_bot_token='123456:synthetic', telegram_user_id=123,
                        telegram_chat_id=123, openai_model='test')
    app = bot.build_bot(db, settings)
    return settings, app


def update(text, mid=10):
    return SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123),
        message=SimpleNamespace(text=text, message_id=mid, reply_to_message=None, reply_text=AsyncMock()))


def command(app, name):
    return next(h.callback for h in app.handlers[0] if name in getattr(h, 'commands', []))


@pytest.mark.parametrize('instant', ['2026-09-12T06:00:00+00:00', '2026-12-12T07:00:00+00:00'])
async def test_daily_run_once_with_prague_dst_and_restart(db, monkeypatch, instant):
    settings, _ = handlers(db)
    collect = AsyncMock(return_value={'new': 2})
    evaluate = AsyncMock(return_value={'evaluation_state': 'complete', 'remaining': 0})
    monkeypatch.setattr(scheduler, 'collect_jobs', collect)
    monkeypatch.setattr(scheduler, 'evaluate_queue', evaluate)
    queue = Mock()
    monkeypatch.setattr(scheduler, 'build_queue', queue)
    monkeypatch.setattr(scheduler, 'dispatch', AsyncMock())
    now = datetime.fromisoformat(instant)
    await scheduler.daily_cycle(db, settings, None, None, now=now.replace(hour=now.hour - 1))
    collect.assert_not_called()
    await scheduler.daily_cycle(db, settings, None, None, now=now)
    await scheduler.daily_cycle(db, settings, None, None, now=now.replace(hour=now.hour + 1))
    collect.assert_awaited_once()
    evaluate.assert_awaited_once()
    queue.assert_called_once()


async def test_evaluation_retry_does_not_repeat_collection(db, monkeypatch):
    settings, _ = handlers(db)
    collect = AsyncMock(return_value={'new': 2})
    evaluate = AsyncMock(side_effect=[{'evaluation_state': 'deadline_reached_partial', 'remaining': 2},
                                     {'evaluation_state': 'complete', 'remaining': 0}])
    monkeypatch.setattr(scheduler, 'collect_jobs', collect)
    monkeypatch.setattr(scheduler, 'evaluate_queue', evaluate)
    queue = Mock()
    monkeypatch.setattr(scheduler, 'build_queue', queue)
    monkeypatch.setattr(scheduler, 'dispatch', AsyncMock())
    await scheduler.daily_cycle(db, settings, None, None, datetime.fromisoformat('2026-09-12T06:00:00+00:00'))
    queue.assert_not_called()
    await scheduler.daily_cycle(db, settings, None, None, datetime.fromisoformat('2026-09-12T06:31:00+00:00'))
    collect.assert_awaited_once()
    assert evaluate.await_count == 2
    queue.assert_called_once()
    with db() as session:
        assert len(session.scalars(select(Delivery).where(Delivery.kind == 'notice')).all()) == 1


async def test_collection_timeout_does_not_skip_evaluation(monkeypatch):
    monkeypatch.setattr(worker, 'collect_jobs', AsyncMock(return_value={'state': 'deadline_reached_partial'}))
    evaluate = AsyncMock(return_value={'evaluated': 3})
    monkeypatch.setattr(worker, 'evaluate_queue', evaluate)
    result = await worker.scan(None, None)
    assert result['evaluated'] == 3
    evaluate.assert_awaited_once()


async def test_rules_require_explicit_confirmation_and_can_be_removed(db):
    settings, app = handlers(db)
    original = fingerprint(runtime_preferences(db, settings))
    request = update('/rule Не показывать вакансии с обязательным турецким.')
    await command(app, 'rule')(request, None)
    assert fingerprint(runtime_preferences(db, settings)) == original
    callback = next(h.callback for h in app.handlers[0] if type(h).__name__ == 'CallbackQueryHandler')
    request.callback_query = SimpleNamespace(data='ruleyes:10', answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock()))
    await callback(request, None)
    assert 'обязательным турецким' in runtime_preferences(db, settings).policy_text
    assert fingerprint(runtime_preferences(db, settings)) != original
    request.callback_query.data = 'ruledelete:10'
    await callback(request, None)
    assert fingerprint(runtime_preferences(db, settings)) == original


async def test_comment_button_stores_exact_reply_after_bot_restart(db):
    settings, app = handlers(db)
    with db.begin() as session:
        session.add(Job(id='job', canonical_key='key', company='Example', title='Website Manager',
                        location='Prague', canonical_url='https://example.com/job', current_hash='hash'))
        session.add(Version(id='version', job_id='job', content_hash='hash', payload={}))
        session.flush()
        session.add(Delivery(chat_id='123', slot='day', kind='job:job', version_id='version',
                             body='Card', message_id=5))
    request = update('/ignored')
    reply = AsyncMock(return_value=SimpleNamespace(message_id=20))
    request.callback_query = SimpleNamespace(data='comment:job', answer=AsyncMock(),
        message=SimpleNamespace(message_id=5, reply_text=reply))
    callback = next(h.callback for h in app.handlers[0] if type(h).__name__ == 'CallbackQueryHandler')
    await callback(request, None)
    _, restarted = handlers(db)
    text = 'Интересно, но именно здесь слишком много SEO.'
    response = update(text, 21)
    handler = next(h.callback for h in restarted.handlers[0] if type(h).__name__ == 'MessageHandler')
    await handler(response, None)
    with db() as session:
        feedback = session.scalar(select(Feedback))
        assert feedback.comment == text
        assert feedback.job_id == 'job'
        assert not session.scalars(select(State).where(State.key.like('rule:%'))).all()
    assert runtime_preferences(db, settings).policy_text == load_preferences(settings).policy_text


def test_old_private_schedule_migrates_without_recopying_bundle():
    import json

    from test_private_config import sample_bundle

    bundle = sample_bundle()
    bundle['preferences']['schedule']['delivery_times'] = ['08:00', '13:00', '18:00']
    prefs = load_preferences(Settings(private_config_json=json.dumps(bundle)))
    assert prefs.schedule.delivery_times == ['08:00']


def test_card_is_short_and_does_not_show_score_or_duplicate_links(assessment):
    from job_monitor.delivery import card

    result = assessment.model_dump()
    result['why_it_fits'] = ['Detailed evidence ' * 100] * 4
    result['gaps'] = ['Long risk ' * 100] * 6
    job = SimpleNamespace(company='Example', title='Website Manager', location='Czechia',
                          official_url='https://example.com/job', canonical_url='https://example.com/job')
    text = card(job, SimpleNamespace(payload={}), SimpleNamespace(result=result, category='STRETCH', score=64))
    assert len(text) < 1100
    assert '64/100' not in text
    assert text.count('https://example.com/job') == 1
    assert 'CORE_WEB' not in text
