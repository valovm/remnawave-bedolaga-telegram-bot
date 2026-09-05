import asyncio
from dataclasses import dataclass
from typing import Any

import structlog
from aiogram import Bot, types

from app.config import settings
from app.database.crud.user import get_user_by_telegram_id
from app.database.database import AsyncSessionLocal
from app.localization.texts import get_texts
from app.utils.miniapp_buttons import build_cabinet_url


logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OnboardingPost:
    key: str
    description: str
    delay_seconds: int
    text_key: str
    movo_button_text_key: str | None = None
    show_support_button: bool = False
    choices: tuple[tuple[str, str], ...] = ()


ONBOARDING_POSTS = (
    OnboardingPost(
        key='welcome',
        description='Вступительный пост сразу после /start для пользователя без активной подписки',
        delay_seconds=0,
        text_key='MOVO_ONBOARDING_WELCOME_TEXT',
        movo_button_text_key='MOVO_ONBOARDING_CONNECT_BUTTON',
    ),
    OnboardingPost(
        key='connection_reminder',
        description='Первый дожим: помощь с подключением через 20 минут после /start',
        delay_seconds=20 * 60,
        text_key='MOVO_ONBOARDING_CONNECTION_REMINDER_TEXT',
        movo_button_text_key='MOVO_ONBOARDING_OPEN_BUTTON',
        show_support_button=True,
    ),
    OnboardingPost(
        key='faq_reminder',
        description='Второй дожим: ответы на частые вопросы через 3 часа после /start',
        delay_seconds=3 * 60 * 60,
        text_key='MOVO_ONBOARDING_FAQ_REMINDER_TEXT',
        movo_button_text_key='MOVO_ONBOARDING_RETRY_BUTTON',
        show_support_button=True,
    ),
    OnboardingPost(
        key='reactivation_survey',
        description='Реактивация: опрос о причине отказа через 3 дня после /start',
        delay_seconds=3 * 24 * 60 * 60,
        text_key='MOVO_ONBOARDING_REACTIVATION_TEXT',
        choices=(
            ('MOVO_ONBOARDING_REASON_NO_TIME', 'no_time'),
            ('MOVO_ONBOARDING_REASON_NOT_CLEAR', 'not_clear'),
            ('MOVO_ONBOARDING_REASON_ERROR', 'error'),
            ('MOVO_ONBOARDING_REASON_NOT_NEEDED', 'not_needed'),
        ),
    ),
)

REACTION_RESPONSES = {
    'no_time': ('MOVO_ONBOARDING_NO_TIME_RESPONSE', ('offer',)),
    'not_clear': ('MOVO_ONBOARDING_NOT_CLEAR_RESPONSE', ('instruction', 'support')),
    'error': ('MOVO_ONBOARDING_ERROR_RESPONSE', ('contact_support',)),
    'not_needed': ('MOVO_ONBOARDING_NOT_NEEDED_RESPONSE', ('retry', 'decline')),
}


class MovoStartService:
    """MOVO-specific /start flow kept outside the upstream bot handlers."""

    def __init__(self, posts: tuple[OnboardingPost, ...] = ONBOARDING_POSTS) -> None:
        self.posts = posts
        self._followup_tasks: dict[int, asyncio.Task[None]] = {}

    @staticmethod
    def has_active_subscription(user: Any | None) -> bool:
        if user is None:
            return False

        subscriptions = getattr(user, 'subscriptions', None) or []
        return any(
            getattr(subscription, 'is_active', False) or getattr(subscription, 'actual_status', None) == 'limited'
            for subscription in subscriptions
        )

    async def handle(self, message: types.Message, user: Any | None) -> bool:
        """Send MOVO's first post and report whether the custom branch ran."""
        if self.has_active_subscription(user):
            return False

        welcome_post = self.posts[0]
        await message.answer(self._post_text(welcome_post), reply_markup=self._build_keyboard(welcome_post))
        self.schedule_followup(message.bot, message.chat.id, message.from_user.id)
        return True

    def schedule_followup(self, bot: Bot, chat_id: int, telegram_id: int) -> None:
        previous_task = self._followup_tasks.get(telegram_id)
        if previous_task and not previous_task.done():
            previous_task.cancel()

        task = asyncio.create_task(self._run_followup(bot, chat_id, telegram_id))
        self._followup_tasks[telegram_id] = task
        task.add_done_callback(lambda completed, user_id=telegram_id: self._forget_task(user_id, completed))

    def _forget_task(self, telegram_id: int, completed_task: asyncio.Task[None]) -> None:
        if self._followup_tasks.get(telegram_id) is completed_task:
            self._followup_tasks.pop(telegram_id, None)

    async def _run_followup(self, bot: Bot, chat_id: int, telegram_id: int) -> None:
        try:
            elapsed_seconds = 0
            for post in self.posts[1:]:
                await asyncio.sleep(max(0, post.delay_seconds - elapsed_seconds))
                if not await self._send_post_if_needed(bot, chat_id, telegram_id, post=post):
                    return
                elapsed_seconds = post.delay_seconds
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Не удалось выполнить цепочку MOVO-постов', telegram_id=telegram_id)

    async def send_followup_if_needed(self, bot: Bot, chat_id: int, telegram_id: int) -> bool:
        return await self._send_post_if_needed(bot, chat_id, telegram_id, post=self.posts[1])

    async def send_second_followup_if_needed(self, bot: Bot, chat_id: int, telegram_id: int) -> bool:
        return await self._send_post_if_needed(bot, chat_id, telegram_id, post=self.posts[2])

    async def handle_reaction(self, callback: types.CallbackQuery) -> bool:
        prefix = 'movo_onboarding:'
        data = callback.data or ''
        if not data.startswith(prefix):
            return False

        action = data.removeprefix(prefix)
        if action == 'decline':
            await callback.answer(get_texts('ru').t('MOVO_ONBOARDING_DECLINED_NOTICE'))
            return True

        response = REACTION_RESPONSES.get(action)
        if response is None:
            await callback.answer()
            return True

        text_key, button_actions = response
        await callback.message.answer(
            get_texts('ru').t(text_key),
            reply_markup=self._build_reaction_keyboard(button_actions),
        )
        await callback.answer()
        return True

    async def _send_post_if_needed(
        self,
        bot: Bot,
        chat_id: int,
        telegram_id: int,
        *,
        post: OnboardingPost,
    ) -> bool:
        async with AsyncSessionLocal() as db:
            user = await get_user_by_telegram_id(db, telegram_id)
            if self.has_active_subscription(user):
                return False

        await bot.send_message(
            chat_id=chat_id,
            text=self._post_text(post),
            reply_markup=self._build_keyboard(post),
        )
        return True

    @staticmethod
    def _build_keyboard(post: OnboardingPost) -> types.InlineKeyboardMarkup:
        texts = get_texts('ru')
        rows: list[list[types.InlineKeyboardButton]] = []
        for label_key, action in post.choices:
            rows.append(
                [
                    types.InlineKeyboardButton(
                        text=texts.t(label_key),
                        callback_data=f'movo_onboarding:{action}',
                    )
                ]
            )
        if post.show_support_button:
            support_url = settings.get_support_contact_url()
            rows.append(
                [
                    types.InlineKeyboardButton(text=texts.t('MOVO_ONBOARDING_SUPPORT_BUTTON'), url=support_url)
                    if support_url
                    else types.InlineKeyboardButton(
                        text=texts.t('MOVO_ONBOARDING_SUPPORT_BUTTON'), callback_data='menu_support'
                    )
                ]
            )

        if post.movo_button_text_key:
            cabinet_url = build_cabinet_url('/subscription')
            movo_button_text = texts.t(post.movo_button_text_key)
            rows.append(
                [
                    types.InlineKeyboardButton(text=movo_button_text, web_app=types.WebAppInfo(url=cabinet_url))
                    if cabinet_url
                    else types.InlineKeyboardButton(text=movo_button_text, callback_data='menu_subscription')
                ]
            )
        return types.InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _build_reaction_keyboard(actions: tuple[str, ...]) -> types.InlineKeyboardMarkup:
        texts = get_texts('ru')
        support_url = settings.get_support_contact_url()
        cabinet_url = build_cabinet_url('/subscription')
        rows: list[list[types.InlineKeyboardButton]] = []
        for action in actions:
            if action == 'support':
                button = (
                    types.InlineKeyboardButton(text=texts.t('MOVO_ONBOARDING_SUPPORT_BUTTON'), url=support_url)
                    if support_url
                    else types.InlineKeyboardButton(
                        text=texts.t('MOVO_ONBOARDING_SUPPORT_BUTTON'), callback_data='menu_support'
                    )
                )
            elif action == 'contact_support':
                button = (
                    types.InlineKeyboardButton(text=texts.t('MOVO_ONBOARDING_CONTACT_SUPPORT_BUTTON'), url=support_url)
                    if support_url
                    else types.InlineKeyboardButton(
                        text=texts.t('MOVO_ONBOARDING_CONTACT_SUPPORT_BUTTON'), callback_data='menu_support'
                    )
                )
            elif action == 'decline':
                button = types.InlineKeyboardButton(
                    text=texts.t('MOVO_ONBOARDING_DECLINE_BUTTON'), callback_data='movo_onboarding:decline'
                )
            else:
                label_key = {
                    'offer': 'MOVO_ONBOARDING_MONTH_OFFER_BUTTON',
                    'instruction': 'MOVO_ONBOARDING_INSTRUCTION_BUTTON',
                    'retry': 'MOVO_ONBOARDING_TRY_ANYWAY_BUTTON',
                }[action]
                button = (
                    types.InlineKeyboardButton(text=texts.t(label_key), web_app=types.WebAppInfo(url=cabinet_url))
                    if cabinet_url
                    else types.InlineKeyboardButton(text=texts.t(label_key), callback_data='menu_subscription')
                )
            rows.append([button])
        return types.InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _post_text(post: OnboardingPost) -> str:
        return get_texts('ru').t(post.text_key)


movo_start_service = MovoStartService()
