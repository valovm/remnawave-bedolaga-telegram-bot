"""Mixin для интеграции с OplateX (docs.oplatex.com)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import PaymentMethod, TransactionType
from app.services.oplatex_service import oplatex_service
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Маппинг статусов OplateX -> internal.
# Документация не перечисляет все состояния — известны 'created' и 'confirmed';
# остальные добавлены защитно. Неизвестный статус трактуется как pending
# (никогда не фейлим по неизвестному состоянию — polling сможет дообработать).
OPLATEX_STATUS_MAP: dict[str, tuple[str, bool]] = {
    'created': ('pending', False),
    'builded': ('pending', False),
    'pending': ('pending', False),
    'processing': ('processing', False),
    'confirmed': ('success', True),
    'completed': ('success', True),
    'success': ('success', True),
    'cancelled': ('canceled', False),
    'canceled': ('canceled', False),
    'expired': ('expired', False),
    'failed': ('failed', False),
}


def map_oplatex_state(state: str | None) -> tuple[str, bool]:
    """Возвращает (internal_status, is_paid) для статуса OplateX."""
    normalized = (state or '').strip().lower()
    status_info = OPLATEX_STATUS_MAP.get(normalized)
    if status_info is None:
        logger.warning('OplateX: неизвестный статус сделки, трактуем как pending', state=state)
        return ('pending', False)
    return status_info


class OplateXPaymentMixin:
    """Mixin для работы с платежами OplateX."""

    async def create_oplatex_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str = 'Пополнение баланса',
        email: str | None = None,
        language: str = 'ru',
        return_url: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Создает платеж OplateX.

        Returns:
            Словарь с данными платежа или None при ошибке
        """
        if not settings.is_oplatex_enabled():
            logger.error('OplateX не настроен')
            return None

        # Валидация лимитов
        if amount_kopeks < settings.OPLATEX_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'OplateX: сумма меньше минимальной',
                amount_kopeks=amount_kopeks,
                OPLATEX_MIN_AMOUNT_KOPEKS=settings.OPLATEX_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.OPLATEX_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'OplateX: сумма больше максимальной',
                amount_kopeks=amount_kopeks,
                OPLATEX_MAX_AMOUNT_KOPEKS=settings.OPLATEX_MAX_AMOUNT_KOPEKS,
            )
            return None

        # Получаем telegram_id пользователя для order_id
        payment_module = import_module('app.services.payment_service')
        if user_id is not None:
            user = await payment_module.get_user_by_id(db, user_id)
            tg_id = user.telegram_id if user else user_id
        else:
            user = None
            tg_id = 'guest'

        # Генерируем уникальный order_id с telegram_id для удобного поиска
        order_id = f'ox{tg_id}_{uuid.uuid4().hex[:6]}'
        amount_rubles = amount_kopeks / 100
        currency = settings.OPLATEX_CURRENCY
        payment_type = settings.OPLATEX_PAYMENT_TYPE

        # Метаданные
        metadata = {
            'user_id': user_id,
            'amount_kopeks': amount_kopeks,
            'description': description,
            'language': language,
            'type': 'balance_topup',
        }

        try:
            # Передаём только обязательные параметры; вебхук настраивается
            # в кабинете OplateX (callback_url/return_url не отправляем)
            result = await oplatex_service.create_payment(
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                payment_type=payment_type,
                customer=str(tg_id),
            )

            payment_data = result.get('payment_data') or {}
            payment_url = payment_data.get('url') if isinstance(payment_data, dict) else None
            oplatex_id = result.get('id')

            if not payment_url:
                logger.error('OplateX API не вернул payment_data.url', result=result)
                return None

            # Сохраняем данные ответа (реквизиты, курс) в метаданных
            metadata['oplatex_payment_data'] = payment_data
            if result.get('rate') is not None:
                metadata['oplatex_rate'] = result.get('rate')

            logger.info(
                'OplateX API: создан платеж',
                order_id=order_id,
                oplatex_id=oplatex_id,
                payment_url=payment_url,
            )

            # Срок действия — 30 минут по умолчанию
            expires_at = datetime.now(UTC) + timedelta(minutes=30)

            # Сохраняем в БД
            oplatex_crud = import_module('app.database.crud.oplatex')
            local_payment = await oplatex_crud.create_oplatex_payment(
                db=db,
                user_id=user_id,
                order_id=order_id,
                amount_kopeks=amount_kopeks,
                currency=currency,
                description=description,
                payment_url=payment_url,
                payment_method=payment_type,
                oplatex_id=oplatex_id,
                expires_at=expires_at,
                metadata_json=metadata,
            )

            logger.info(
                'OplateX: создан платеж',
                order_id=order_id,
                user_id=user_id,
                amount_rubles=amount_rubles,
                currency=currency,
            )

            return {
                'order_id': order_id,
                'oplatex_id': oplatex_id,
                'amount_kopeks': amount_kopeks,
                'amount_rubles': amount_rubles,
                'currency': currency,
                'payment_url': payment_url,
                'expires_at': expires_at.isoformat(),
                'local_payment_id': local_payment.id,
            }

        except Exception as e:
            logger.exception('OplateX: ошибка создания платежа', error=e)
            return None

    async def process_oplatex_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        """
        Обрабатывает webhook от OplateX.

        Подпись (заголовок Signature, HMAC-SHA256 сырого тела) проверяется
        в webserver/payments.py до вызова этого метода. Тело вебхука — trade JSON:
        {id, order, merchant, state, amount_cents, amount_currency, rate, payment_data}.
        Живой API использует другие имена полей, чем в доке (order_id/cents/currency
        вместо order/amount_cents/amount_currency) — принимаем оба варианта.

        Returns:
            True если платеж успешно обработан
        """
        try:
            oplatex_id = payload.get('id')
            order_id = payload.get('order') or payload.get('order_id')
            state = payload.get('state')

            if not oplatex_id or not state:
                logger.warning('OplateX webhook: отсутствуют обязательные поля', payload=payload)
                return False

            # Ищем платеж по order_id (наш) или oplatex_id
            oplatex_crud = import_module('app.database.crud.oplatex')
            payment = None
            if order_id:
                payment = await oplatex_crud.get_oplatex_payment_by_order_id(db, order_id)
            if not payment and oplatex_id:
                payment = await oplatex_crud.get_oplatex_payment_by_oplatex_id(db, oplatex_id)

            if not payment:
                logger.warning(
                    'OplateX webhook: платеж не найден',
                    order_id=order_id,
                    oplatex_id=oplatex_id,
                )
                return False

            # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
            locked = await oplatex_crud.get_oplatex_payment_by_id_for_update(db, payment.id)
            if not locked:
                logger.error('OplateX: не удалось заблокировать платёж', payment_id=payment.id)
                return False
            payment = locked

            # Проверка дублирования (re-check from locked row)
            if payment.is_paid:
                logger.info('OplateX webhook: платеж уже обработан', order_id=payment.order_id)
                return True

            internal_status, is_paid = map_oplatex_state(state)

            received_kopeks = payload.get('amount_cents')
            if received_kopeks is None:
                received_kopeks = payload.get('cents')
            received_currency = payload.get('amount_currency') or payload.get('currency')

            callback_payload = {
                'oplatex_id': oplatex_id,
                'order_id': order_id,
                'state': state,
                'amount_cents': received_kopeks,
                'amount_currency': received_currency,
                'rate': payload.get('rate'),
            }

            # Проверка суммы и валюты ДО обновления статуса — строгое равенство
            if is_paid:
                if received_kopeks is not None and int(received_kopeks) != payment.amount_kopeks:
                    logger.error(
                        'OplateX amount mismatch',
                        expected_kopeks=payment.amount_kopeks,
                        received_kopeks=received_kopeks,
                        order_id=payment.order_id,
                    )
                    await oplatex_crud.update_oplatex_payment_status(
                        db=db,
                        payment=payment,
                        status='amount_mismatch',
                        is_paid=False,
                        oplatex_id=oplatex_id,
                        callback_payload=callback_payload,
                    )
                    return False
                if received_currency is not None and received_currency != payment.currency:
                    logger.error(
                        'OplateX currency mismatch',
                        expected_currency=payment.currency,
                        received_currency=received_currency,
                        order_id=payment.order_id,
                    )
                    await oplatex_crud.update_oplatex_payment_status(
                        db=db,
                        payment=payment,
                        status='amount_mismatch',
                        is_paid=False,
                        oplatex_id=oplatex_id,
                        callback_payload=callback_payload,
                    )
                    return False

            # Финализируем платеж если оплачен — без промежуточного commit
            if is_paid:
                # Inline field assignments to keep FOR UPDATE lock intact
                payment.status = internal_status
                payment.is_paid = True
                payment.paid_at = datetime.now(UTC)
                payment.oplatex_id = oplatex_id or payment.oplatex_id
                payment.callback_payload = callback_payload
                payment.updated_at = datetime.now(UTC)
                await db.flush()
                return await self._finalize_oplatex_payment(db, payment, oplatex_id=oplatex_id, trigger='webhook')

            # Для не-success статусов можно безопасно коммитить
            payment = await oplatex_crud.update_oplatex_payment_status(
                db=db,
                payment=payment,
                status=internal_status,
                is_paid=False,
                oplatex_id=oplatex_id,
                callback_payload=callback_payload,
            )

            return True

        except Exception as e:
            logger.exception('OplateX webhook: ошибка обработки', error=e)
            return False

    async def _finalize_oplatex_payment(
        self,
        db: AsyncSession,
        payment: Any,
        *,
        oplatex_id: str | None,
        trigger: str,
    ) -> bool:
        """Создаёт транзакцию, начисляет баланс и отправляет уведомления.

        FOR UPDATE lock must be acquired by the caller before invoking this method.
        """
        payment_module = import_module('app.services.payment_service')
        oplatex_crud = import_module('app.database.crud.oplatex')

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'OplateX платеж уже связан с транзакцией',
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                trigger=trigger,
            )
            return True

        # Read fresh metadata AFTER lock to avoid stale data
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        # --- Guest purchase flow ---
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=str(oplatex_id) if oplatex_id else payment.order_id,
            provider_name='oplatex',
        )
        if guest_result is not None:
            return True

        # Ensure paid fields are set (idempotent — caller may have already set them)
        if not payment.is_paid:
            payment.status = 'success'
            payment.is_paid = True
            payment.paid_at = datetime.now(UTC)
            payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для OplateX', user_id=payment.user_id)
            return False

        # Загружаем промогруппы в асинхронном контексте
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = str(oplatex_id) if oplatex_id else payment.order_id

        # Проверяем дупликат транзакции
        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.OPLATEX,
            )

        display_name = settings.get_oplatex_display_name()
        description = f'Пополнение через {display_name}'

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.OPLATEX,
                external_id=transaction_external_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await oplatex_crud.link_oplatex_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('OplateX платеж уже зачислил баланс ранее', order_id=payment.order_id)
            return True

        # Lock user row to prevent concurrent balance race conditions
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.OPLATEX,
            external_id=transaction_external_id,
        )

        topup_status = '\U0001f195 Первое пополнение' if was_first_topup else '\U0001f504 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения OplateX', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                )
            except Exception as error:
                logger.error('Ошибка отправки админ уведомления OplateX', error=error)

        if getattr(self, 'bot', None) and user.telegram_id:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'\U0001f4b0 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'\U0001f4b3 Способ: {display_name}\n'
                        f'\U0001f194 Транзакция: {transaction.id}\n\n'
                        'Баланс пополнен автоматически!'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю OplateX', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя',
                user_id=payment.user_id,
                error=error,
                exc_info=True,
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        payment.metadata_json = metadata
        await db.commit()

        logger.info(
            'Обработан OplateX платеж',
            order_id=payment.order_id,
            user_id=payment.user_id,
            trigger=trigger,
        )

        return True

    async def check_oplatex_payment_status(
        self,
        db: AsyncSession,
        order_id: str,
    ) -> dict[str, Any] | None:
        """Проверяет статус платежа через API."""
        try:
            oplatex_crud = import_module('app.database.crud.oplatex')
            payment = await oplatex_crud.get_oplatex_payment_by_order_id(db, order_id)
            if not payment:
                logger.warning('OplateX payment not found', order_id=order_id)
                return None

            if payment.is_paid:
                return {
                    'payment': payment,
                    'status': 'success',
                    'is_paid': True,
                }

            # Проверяем через API по oplatex_id (trade UUID)
            if payment.oplatex_id:
                try:
                    trade_data = await oplatex_service.get_trade(payment.oplatex_id)
                    state = trade_data.get('state')

                    if state:
                        internal_status, is_paid = map_oplatex_state(state)

                        if is_paid:
                            # Проверка суммы — строгое равенство amount_cents/cents
                            api_amount = trade_data.get('amount_cents')
                            if api_amount is None:
                                api_amount = trade_data.get('cents')
                            if api_amount is not None and int(api_amount) != payment.amount_kopeks:
                                logger.error(
                                    'OplateX amount mismatch (API check)',
                                    expected_kopeks=payment.amount_kopeks,
                                    received_kopeks=api_amount,
                                    order_id=payment.order_id,
                                )
                                await oplatex_crud.update_oplatex_payment_status(
                                    db=db,
                                    payment=payment,
                                    status='amount_mismatch',
                                    is_paid=False,
                                    oplatex_id=payment.oplatex_id,
                                    callback_payload={
                                        'check_source': 'api',
                                        'oplatex_trade_data': trade_data,
                                    },
                                )
                                return {
                                    'payment': payment,
                                    'status': 'amount_mismatch',
                                    'is_paid': False,
                                }

                            # Acquire FOR UPDATE lock before finalization
                            locked = await oplatex_crud.get_oplatex_payment_by_id_for_update(db, payment.id)
                            if not locked:
                                logger.error('OplateX: не удалось заблокировать платёж', payment_id=payment.id)
                                return None
                            payment = locked

                            if payment.is_paid:
                                logger.info('OplateX платеж уже обработан (api_check)', order_id=payment.order_id)
                                return {
                                    'payment': payment,
                                    'status': 'success',
                                    'is_paid': True,
                                }

                            logger.info('OplateX payment confirmed via API', order_id=payment.order_id)

                            # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
                            payment.status = 'success'
                            payment.is_paid = True
                            payment.paid_at = datetime.now(UTC)
                            payment.callback_payload = {
                                'check_source': 'api',
                                'oplatex_trade_data': trade_data,
                            }
                            payment.updated_at = datetime.now(UTC)
                            await db.flush()

                            await self._finalize_oplatex_payment(
                                db,
                                payment,
                                oplatex_id=payment.oplatex_id,
                                trigger='api_check',
                            )
                        elif internal_status != payment.status:
                            # Обновляем статус если изменился
                            payment = await oplatex_crud.update_oplatex_payment_status(
                                db=db,
                                payment=payment,
                                status=internal_status,
                            )

                except Exception as e:
                    logger.error('Error checking OplateX payment status via API', error=e)

            return {
                'payment': payment,
                'status': payment.status or 'pending',
                'is_paid': payment.is_paid,
            }

        except Exception as e:
            logger.exception('OplateX: ошибка проверки статуса', error=e)
            return None
