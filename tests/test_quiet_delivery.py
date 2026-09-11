from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from job_monitor import delivery
from job_monitor.models import Base, Delivery


@pytest.fixture
def quiet_factory(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)

    @contextmanager
    def lock(factory, name):
        with factory.begin() as session:
            yield session

    monkeypatch.setattr(delivery, 'transaction_lock', lock)
    yield factory
    engine.dispose()


async def test_empty_scheduled_digest_is_silent_and_idempotent(quiet_factory, settings, preferences):
    for _ in range(2):
        delivery.build_queue(quiet_factory, settings, preferences, '2026-09-12T08:00')
    bot = AsyncMock()
    await delivery.dispatch(quiet_factory, bot, AsyncMock())
    bot.send_message.assert_not_awaited()
    with quiet_factory() as session:
        rows = session.scalars(select(Delivery)).all()
        assert len(rows) == 1
        assert rows[0].status == 'suppressed'


async def test_old_pending_scheduled_report_is_not_sent(quiet_factory):
    with quiet_factory.begin() as session:
        session.add(Delivery(chat_id='123', slot='2026-09-11T18:00', kind='summary', body='Old report'))
    bot = AsyncMock()
    await delivery.dispatch(quiet_factory, bot, AsyncMock())
    bot.send_message.assert_not_awaited()
    with quiet_factory() as session:
        assert session.scalar(select(Delivery.status)) == 'suppressed'


async def test_manual_empty_request_still_gets_answer(quiet_factory, settings, preferences):
    delivery.build_queue(quiet_factory, settings, preferences, 'manual:123:12')
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 99
    await delivery.dispatch(quiet_factory, bot, AsyncMock())
    bot.send_message.assert_awaited_once()
    assert '/scan' in bot.send_message.call_args.kwargs['text']
