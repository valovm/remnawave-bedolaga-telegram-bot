from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.localization.texts import get_texts
from app.services.movo_start_service import ONBOARDING_POSTS, MovoStartService


@pytest.mark.asyncio
async def test_sends_welcome_post_with_cabinet_miniapp_button_when_user_has_no_active_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(
        answer=AsyncMock(),
        bot=SimpleNamespace(),
        chat=SimpleNamespace(id=100),
        from_user=SimpleNamespace(id=200),
    )
    monkeypatch.setattr(
        'app.services.movo_start_service.build_cabinet_url',
        lambda path: f'https://cabinet.example{path}',
    )

    service = MovoStartService()
    service.schedule_followup = Mock()

    handled = await service.handle(message, SimpleNamespace(subscriptions=[]))

    assert handled is True
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == get_texts('ru').t(ONBOARDING_POSTS[0].text_key)
    button = message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.text == '🚀 Подключить VPN'
    assert button.web_app.url == 'https://cabinet.example/subscription'
    assert button.callback_data is None
    service.schedule_followup.assert_called_once_with(message.bot, 100, 200)


@pytest.mark.asyncio
async def test_skips_welcome_post_when_user_has_active_subscription() -> None:
    message = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(subscriptions=[SimpleNamespace(is_active=True, actual_status='active')])

    handled = await MovoStartService().handle(message, user)

    assert handled is False
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_treats_limited_subscription_as_active() -> None:
    message = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(subscriptions=[SimpleNamespace(is_active=False, actual_status='limited')])

    handled = await MovoStartService().handle(message, user)

    assert handled is False
    message.answer.assert_not_awaited()


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_followup_is_sent_after_fresh_check_confirms_no_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(subscriptions=[])
    monkeypatch.setattr('app.services.movo_start_service.build_cabinet_url', lambda path: f'https://movo.test{path}')

    with (
        patch('app.services.movo_start_service.AsyncSessionLocal', return_value=_SessionContext()),
        patch('app.services.movo_start_service.get_user_by_telegram_id', new=AsyncMock(return_value=user)),
        patch('app.services.movo_start_service.settings') as settings_mock,
    ):
        settings_mock.get_support_contact_url.return_value = 'https://t.me/help'
        sent = await MovoStartService().send_followup_if_needed(bot, chat_id=100, telegram_id=200)

    assert sent is True
    call = bot.send_message.await_args.kwargs
    assert call['text'] == get_texts('ru').t(ONBOARDING_POSTS[1].text_key)
    assert call['reply_markup'].inline_keyboard[0][0].url == 'https://t.me/help'
    assert call['reply_markup'].inline_keyboard[1][0].web_app.url == 'https://movo.test/subscription'


@pytest.mark.asyncio
async def test_followup_is_cancelled_when_subscription_was_activated() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(subscriptions=[SimpleNamespace(is_active=True, actual_status='active')])

    with (
        patch('app.services.movo_start_service.AsyncSessionLocal', return_value=_SessionContext()),
        patch('app.services.movo_start_service.get_user_by_telegram_id', new=AsyncMock(return_value=user)),
    ):
        sent = await MovoStartService().send_followup_if_needed(bot, chat_id=100, telegram_id=200)

    assert sent is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_followup_uses_retry_button_and_fresh_subscription_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(subscriptions=[])
    monkeypatch.setattr('app.services.movo_start_service.build_cabinet_url', lambda path: f'https://movo.test{path}')

    with (
        patch('app.services.movo_start_service.AsyncSessionLocal', return_value=_SessionContext()),
        patch('app.services.movo_start_service.get_user_by_telegram_id', new=AsyncMock(return_value=user)),
        patch('app.services.movo_start_service.settings') as settings_mock,
    ):
        settings_mock.get_support_contact_url.return_value = 'https://t.me/help'
        sent = await MovoStartService().send_second_followup_if_needed(bot, chat_id=100, telegram_id=200)

    assert sent is True
    call = bot.send_message.await_args.kwargs
    assert call['text'] == get_texts('ru').t(ONBOARDING_POSTS[2].text_key)
    assert call['reply_markup'].inline_keyboard[0][0].text == '🛟 Поддержка'
    retry_button = call['reply_markup'].inline_keyboard[1][0]
    assert retry_button.text == '🚀 Попробовать снова'
    assert retry_button.web_app.url == 'https://movo.test/subscription'


@pytest.mark.asyncio
async def test_reactivation_post_contains_four_reason_callbacks() -> None:
    post = ONBOARDING_POSTS[3]
    keyboard = MovoStartService._build_keyboard(post)

    assert post.delay_seconds == 3 * 24 * 60 * 60
    assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
        'movo_onboarding:no_time',
        'movo_onboarding:not_clear',
        'movo_onboarding:error',
        'movo_onboarding:not_needed',
    ]


@pytest.mark.asyncio
async def test_no_time_reaction_opens_month_offer_in_miniapp(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = SimpleNamespace(
        data='movo_onboarding:no_time',
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    monkeypatch.setattr('app.services.movo_start_service.build_cabinet_url', lambda path: f'https://movo.test{path}')

    handled = await MovoStartService().handle_reaction(callback)

    assert handled is True
    response = callback.message.answer.await_args.kwargs
    assert response['reply_markup'].inline_keyboard[0][0].text == '🚀 Получить месяц за 1 ₽'
    assert response['reply_markup'].inline_keyboard[0][0].web_app.url == 'https://movo.test/subscription'
