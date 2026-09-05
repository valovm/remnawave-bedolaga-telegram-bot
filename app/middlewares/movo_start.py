from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services.movo_start_service import movo_start_service


class MovoStartMiddleware(BaseMiddleware):
    """Branch /start updates into the isolated MOVO onboarding service."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.text and event.text.startswith('/start'):
            await movo_start_service.handle(event, data.get('db_user'))
        elif isinstance(event, CallbackQuery) and await movo_start_service.handle_reaction(event):
            return None

        return await handler(event, data)
