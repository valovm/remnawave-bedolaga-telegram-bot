import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.handlers.start import cmd_start
from app.services.movo_start_service import movo_start_service


logger = structlog.get_logger(__name__)


async def movo_start(
    message: types.Message,
    state: FSMContext,
    db: AsyncSession,
    db_user=None,
) -> None:
    """Run the MOVO branch first, then preserve the upstream /start flow."""
    try:
        handled = await movo_start_service.handle(message, db_user)
        logger.info(
            'MOVO onboarding /start branch processed',
            telegram_id=message.from_user.id,
            welcome_sent=handled,
        )
    except Exception:
        logger.exception('MOVO onboarding branch failed; continuing regular /start')

    await cmd_start(message, state, db, db_user=db_user)


async def movo_reaction(callback: types.CallbackQuery) -> None:
    await movo_start_service.handle_reaction(callback)


def register_handlers(dp: Dispatcher) -> None:
    dp.message.register(movo_start, Command('start'))
    dp.callback_query.register(movo_reaction, F.data.startswith('movo_onboarding:'))
