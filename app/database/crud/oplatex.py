"""CRUD операции для платежей OplateX."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OplateXPayment


logger = structlog.get_logger(__name__)


async def create_oplatex_payment(
    db: AsyncSession,
    *,
    user_id: int | None,
    order_id: str,
    amount_kopeks: int,
    currency: str = 'RUB',
    description: str | None = None,
    payment_url: str | None = None,
    payment_method: str | None = None,
    oplatex_id: str | None = None,
    expires_at: datetime | None = None,
    metadata_json: dict | None = None,
) -> OplateXPayment:
    """Создает запись о платеже OplateX."""
    payment = OplateXPayment(
        user_id=user_id,
        order_id=order_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        description=description,
        payment_url=payment_url,
        payment_method=payment_method,
        oplatex_id=oplatex_id,
        expires_at=expires_at,
        metadata_json=metadata_json,
        status='pending',
        is_paid=False,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    logger.info('Создан платеж OplateX', order_id=order_id, user_id=user_id)
    return payment


async def get_oplatex_payment_by_order_id(db: AsyncSession, order_id: str) -> OplateXPayment | None:
    """Получает платеж по order_id (internal)."""
    result = await db.execute(select(OplateXPayment).where(OplateXPayment.order_id == order_id))
    return result.scalar_one_or_none()


async def get_oplatex_payment_by_oplatex_id(db: AsyncSession, oplatex_id: str) -> OplateXPayment | None:
    """Получает платеж по trade UUID от OplateX."""
    result = await db.execute(select(OplateXPayment).where(OplateXPayment.oplatex_id == oplatex_id))
    return result.scalar_one_or_none()


async def get_oplatex_payment_by_id(db: AsyncSession, payment_id: int) -> OplateXPayment | None:
    """Получает платеж по ID."""
    result = await db.execute(select(OplateXPayment).where(OplateXPayment.id == payment_id))
    return result.scalar_one_or_none()


async def get_oplatex_payment_by_id_for_update(db: AsyncSession, payment_id: int) -> OplateXPayment | None:
    """Получает платеж по ID с блокировкой FOR UPDATE."""
    result = await db.execute(
        select(OplateXPayment)
        .where(OplateXPayment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def update_oplatex_payment_status(
    db: AsyncSession,
    payment: OplateXPayment,
    *,
    status: str,
    is_paid: bool | None = None,
    oplatex_id: str | None = None,
    payment_method: str | None = None,
    callback_payload: dict | None = None,
    transaction_id: int | None = None,
) -> OplateXPayment:
    """Обновляет статус платежа."""
    payment.status = status
    payment.updated_at = datetime.now(UTC)

    if is_paid is not None:
        payment.is_paid = is_paid
        if is_paid:
            payment.paid_at = datetime.now(UTC)
    if oplatex_id is not None:
        payment.oplatex_id = oplatex_id
    if payment_method is not None:
        payment.payment_method = payment_method
    if callback_payload is not None:
        payment.callback_payload = callback_payload
    if transaction_id is not None:
        payment.transaction_id = transaction_id

    await db.commit()
    await db.refresh(payment)
    logger.info(
        'Обновлен статус платежа OplateX',
        order_id=payment.order_id,
        status=status,
        is_paid=payment.is_paid,
    )
    return payment


async def get_pending_oplatex_payments(db: AsyncSession, user_id: int) -> list[OplateXPayment]:
    """Получает незавершенные платежи пользователя."""
    result = await db.execute(
        select(OplateXPayment).where(
            OplateXPayment.user_id == user_id,
            OplateXPayment.status == 'pending',
            OplateXPayment.is_paid == False,
        )
    )
    return list(result.scalars().all())


async def get_expired_pending_oplatex_payments(
    db: AsyncSession,
) -> list[OplateXPayment]:
    """Получает просроченные платежи в статусе pending."""
    now = datetime.now(UTC)
    result = await db.execute(
        select(OplateXPayment).where(
            OplateXPayment.status == 'pending',
            OplateXPayment.is_paid == False,
            OplateXPayment.expires_at < now,
        )
    )
    return list(result.scalars().all())


async def link_oplatex_payment_to_transaction(
    db: AsyncSession,
    *,
    payment: OplateXPayment,
    transaction_id: int,
) -> OplateXPayment:
    """Связывает платеж с транзакцией."""
    payment.transaction_id = transaction_id
    payment.updated_at = datetime.now(UTC)
    await db.flush()
    await db.refresh(payment)
    return payment
