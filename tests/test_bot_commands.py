from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from job_monitor import bot as module
from job_monitor.config import Settings
from job_monitor.delivery import manual_digest_text


def setup_request(monkeypatch, command, authorized=True, paused=False):
    @contextmanager
    def factory():
        yield SimpleNamespace(get=lambda *args: SimpleNamespace(value={'enabled': True}) if paused else None)

    settings = Settings(telegram_bot_token='123456:synthetic', telegram_user_id=123, telegram_chat_id=123)
    app = module.build_bot(factory, settings)
    reply = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123 if authorized else 999),
        effective_chat=SimpleNamespace(id=123),
        message=SimpleNamespace(text='/' + command, message_id=41, reply_text=reply),
    )
    tasks = []
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={}, create_task=lambda coro, **kwargs: tasks.append(coro)),
        bot=object(),
    )
    handler = next(h.callback for h in app.handlers[0] if command in getattr(h, 'commands', []))
    monkeypatch.setattr(module, 'build_queue', Mock())
    monkeypatch.setattr(module, 'dispatch', AsyncMock())
    monkeypatch.setattr(module, 'Web', lambda: SimpleNamespace(close=AsyncMock()))
    return handler, update, context, tasks, settings


async def test_jobs_sends_existing_queue_without_scan(monkeypatch):
    from job_monitor import worker

    scan = AsyncMock()
    monkeypatch.setattr(worker, 'scan', scan)
    handler, update, context, tasks, settings = setup_request(monkeypatch, 'jobs')
    await handler(update, context)
    assert len(tasks) == 1
    await tasks.pop()
    scan.assert_not_called()
    assert module.build_queue.call_args.args[-1] == 'manual:123:41'
    module.dispatch.assert_awaited_once()
    assert not context.application.bot_data['manual_busy']


async def test_scan_runs_in_background_then_delivers_and_prevents_duplicate(monkeypatch):
    from job_monitor import worker

    scan = AsyncMock(return_value={'evaluated': 1})
    monkeypatch.setattr(worker, 'scan', scan)
    handler, update, context, tasks, settings = setup_request(monkeypatch, 'scan')
    await handler(update, context)
    scan.assert_not_called()
    await handler(update, context)
    assert len(tasks) == 1
    await tasks.pop()
    scan.assert_awaited_once()
    module.dispatch.assert_awaited_once()
    assert not context.application.bot_data['manual_busy']


@pytest.mark.parametrize('authorized,paused', [(False, False), (True, True)])
async def test_commands_respect_authorization_and_pause(monkeypatch, authorized, paused):
    handler, update, context, tasks, settings = setup_request(monkeypatch, 'scan', authorized, paused)
    await handler(update, context)
    assert not tasks
    module.build_queue.assert_not_called()
    if not authorized:
        update.message.reply_text.assert_not_called()


async def test_failed_scan_reports_failure_and_releases_busy(monkeypatch):
    from job_monitor import worker

    monkeypatch.setattr(worker, 'scan', AsyncMock(side_effect=ValueError('secret-marker')))
    handler, update, context, tasks, settings = setup_request(monkeypatch, 'scan')
    await handler(update, context)
    await tasks.pop()
    assert not context.application.bot_data['manual_busy']
    module.dispatch.assert_not_called()
    assert 'secret-marker' not in str(update.message.reply_text.call_args_list)


def test_empty_digest_is_actionable_not_a_claim_all_jobs_were_rejected():
    assert '/scan' in manual_digest_text(0, None)
    assert 'не означает' in manual_digest_text(0, None)
    assert 'Notion' in manual_digest_text(0, 'private-internal-detail')
    assert 'private-internal-detail' not in manual_digest_text(0, 'private-internal-detail')
