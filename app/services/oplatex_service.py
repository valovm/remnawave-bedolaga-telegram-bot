"""Сервис для работы с API OplateX (docs.oplatex.com)."""

import hashlib
import hmac
import json
from typing import Any

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

# Документация для статус-эндпоинта называет заголовок X-Token, но живой API
# на любой запрос отвечает «Check X-Auth header» — заголовок везде X-Auth.
STATUS_AUTH_HEADER = 'X-Auth'


class OplateXAPIError(Exception):
    """Ошибка API OplateX."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f'OplateX API error ({status_code}): {message}')


class OplateXService:
    """Сервис для работы с API OplateX."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    @property
    def merchant_id(self) -> str:
        return settings.OPLATEX_MERCHANT_ID or ''

    @property
    def secret_key(self) -> str:
        return settings.OPLATEX_SECRET_KEY or ''

    @property
    def webhook_secret(self) -> str:
        return settings.OPLATEX_WEBHOOK_SECRET or self.secret_key

    @property
    def api_base_url(self) -> str:
        return (settings.OPLATEX_API_URL or 'https://api.oplatex.com').rstrip('/')

    def _sign_params(self, params: dict[str, Any]) -> str:
        """HMAC-SHA256 подпись параметров запроса.

        База подписи — только ЗНАЧЕНИЯ параметров, отсортированных по имени ключа,
        склеенные через '::'. Ключи в базу не входят; None-поля не подписываются
        (и не должны отправляться).
        """
        base_string = '::'.join(str(params[key]) for key in sorted(params) if params[key] is not None)
        return hmac.new(
            self.secret_key.encode('utf-8'),
            base_string.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Возвращает переиспользуемую HTTP-сессию."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        """Закрывает HTTP-сессию."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def create_payment(
        self,
        *,
        order_id: str,
        amount_kopeks: int,
        currency: str = 'RUB',
        payment_type: str = 'nspk',
        customer: str = 'guest',
    ) -> dict[str, Any]:
        """
        Создает сделку (deposit) через API OplateX.
        POST /api/v1/trades/deposit/build — платёжная страница (payment_data.url).
        Типы: nspk (СБП), any_bank (перевод на карту).

        Отправляем только обязательные параметры, имена полей — как в документации
        (проверено по живому API 29.07.2026; ранее сервер требовал другие имена,
        затем OplateX привели API в соответствие с докой).
        """
        payload: dict[str, Any] = {
            'merchant': self.merchant_id,
            'order': order_id,
            'amount_cents': amount_kopeks,
            'amount_currency': currency,
            'type': payment_type,
            'customer': customer,
        }

        logger.info(
            'OplateX API create_payment',
            order_id=order_id,
            amount_kopeks=amount_kopeks,
            currency=currency,
            payment_type=payment_type,
        )

        try:
            session = await self._get_session()
            async with session.post(
                f'{self.api_base_url}/api/v1/trades/deposit/build',
                json=payload,
                headers={
                    'Content-Type': 'application/json',
                    'X-Auth': self._sign_params(payload),
                },
            ) as response:
                data = await response.json(content_type=None)

                if response.status in (200, 201) and isinstance(data, dict) and data.get('id'):
                    logger.info(
                        'OplateX API trade created',
                        order_id=order_id,
                        oplatex_id=data.get('id'),
                        state=data.get('state'),
                    )
                    return data

                error_msg = (
                    data.get('message') or data.get('error') or str(data) if isinstance(data, dict) else str(data)
                )
                logger.error(
                    'OplateX create_payment error',
                    status_code=response.status,
                    error_msg=error_msg,
                    response_data=data,
                )
                raise OplateXAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('OplateX API connection error', error=e)
            raise

    async def get_trade(self, trade_uuid: str) -> dict[str, Any]:
        """
        Получает информацию о сделке.
        GET /api/v1/trades/:merchant/:uuid
        """
        logger.info('OplateX get_trade', oplatex_id=trade_uuid)

        signature = self._sign_params({'merchant': self.merchant_id, 'uuid': trade_uuid})

        try:
            session = await self._get_session()
            async with session.get(
                f'{self.api_base_url}/api/v1/trades/{self.merchant_id}/{trade_uuid}',
                headers={
                    'Content-Type': 'application/json',
                    STATUS_AUTH_HEADER: signature,
                },
            ) as response:
                data = await response.json(content_type=None)

                if response.status == 200 and isinstance(data, dict) and data.get('id'):
                    return data

                error_msg = (
                    data.get('message') or data.get('error') or str(data) if isinstance(data, dict) else str(data)
                )
                logger.error(
                    'OplateX get_trade error',
                    status_code=response.status,
                    error_msg=error_msg,
                )
                raise OplateXAPIError(response.status, error_msg)

        except aiohttp.ClientError as e:
            logger.exception('OplateX API connection error', error=e)
            raise

    def verify_webhook_signature(self, raw_body: bytes, received_signature: str) -> bool:
        """Верификация вебхука OplateX.

        Заголовок ``Signature`` — HMAC-SHA256 hex, ключ — вебхук-секрет
        (OPLATEX_WEBHOOK_SECRET; это ОТДЕЛЬНЫЙ ключ, не API-токен). База —
        каноническая компактная JSON-сериализация тела: при повторных доставках
        OplateX меняет форматирование (compact/pretty), а подпись остаётся
        прежней, поэтому проверяем и сырое тело, и переупакованный JSON.
        """
        secret = self.webhook_secret
        if not received_signature or not secret:
            return False

        received = received_signature.strip().lower()
        key = secret.encode('utf-8')

        candidates = [raw_body]
        try:
            parsed = json.loads(raw_body)
            canonical = json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)
            candidates.append(canonical.encode('utf-8'))
        except (ValueError, TypeError):
            pass

        for body in candidates:
            expected = hmac.new(key, body, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected, received):
                return True

        logger.warning('OplateX webhook: signature mismatch')
        return False


# Singleton instance
oplatex_service = OplateXService()
