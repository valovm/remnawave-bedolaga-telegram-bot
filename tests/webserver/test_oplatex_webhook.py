"""Tests for the OplateX integration.

Pins the signature contracts (docs.oplatex.com):

* Request auth (``X-Auth`` header): HMAC-SHA256 hex over the request params'
  VALUES ONLY, sorted alphabetically by key, joined with ``::``.
* Webhook (``Signature`` header): HMAC-SHA256 hex of the RAW request body.

Also covers the defensive status map, the amount-mismatch guard and webhook
idempotency in ``process_oplatex_webhook``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.config import settings
from app.services.oplatex_service import OplateXService
from app.services.payment.oplatex import OplateXPaymentMixin, map_oplatex_state
from app.webserver.payments import create_payment_router


SECRET = 'test-oplatex-secret'
WEBHOOK_SECRET = 'test-oplatex-webhook-secret'
MERCHANT = '3fa85f64-5717-4562-b3fc-2c963f66afa6'


@pytest.fixture(autouse=True)
def oplatex_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'OPLATEX_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'OPLATEX_MERCHANT_ID', MERCHANT, raising=False)
    monkeypatch.setattr(settings, 'OPLATEX_SECRET_KEY', SECRET, raising=False)
    monkeypatch.setattr(settings, 'OPLATEX_WEBHOOK_SECRET', WEBHOOK_SECRET, raising=False)
    monkeypatch.setattr(settings, 'OPLATEX_WEBHOOK_PATH', '/oplatex', raising=False)
    monkeypatch.setattr(settings, 'OPLATEX_DISPLAY_NAME', 'OplateX', raising=False)


def _hmac_hex(data: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode('utf-8'), data, hashlib.sha256).hexdigest()


# --- _sign_params: request signature contract ---


def test_sign_params_sorts_keys_and_joins_values_with_double_colon() -> None:
    service = OplateXService()
    params = {
        'merchant': MERCHANT,
        'order': 'ext-123',
        'amount_cents': 1000,
        'amount_currency': 'RUB',
        'type': 'nspk',
        'customer': 'cust-1',
    }
    # Алфавитный порядок ключей: amount_cents, amount_currency, customer, merchant, order, type
    expected_base = f'1000::RUB::cust-1::{MERCHANT}::ext-123::nspk'

    assert service._sign_params(params) == _hmac_hex(expected_base.encode('utf-8'))


def test_sign_params_matches_documentation_example() -> None:
    """Эталонный пример из https://docs.oplatex.com/auth/ (секрет 'pass')."""
    base = '1000::USD::cust-1::3fa85f64-5717-4562-b3fc-2c963f66afa6::ext-123::nspk'
    expected = '2aa0d2adf41a08f164312aafd4925e274e5b75bf4a7bcb66e6d520ffdfdd5255'
    digest = hmac.new(b'pass', base.encode('utf-8'), hashlib.sha256).hexdigest()

    assert digest == expected


def test_sign_params_status_endpoint_base_is_merchant_then_uuid() -> None:
    service = OplateXService()
    trade_uuid = 'aaaa1111-2222-3333-4444-bbbbccccdddd'
    expected_base = f'{MERCHANT}::{trade_uuid}'

    assert service._sign_params({'merchant': MERCHANT, 'uuid': trade_uuid}) == _hmac_hex(expected_base.encode('utf-8'))


def test_sign_params_skips_none_values() -> None:
    service = OplateXService()
    with_none = {'merchant': MERCHANT, 'order': 'o-1', 'customer': None}
    without = {'merchant': MERCHANT, 'order': 'o-1'}

    assert service._sign_params(with_none) == service._sign_params(without)


# --- verify_webhook_signature ---


def _trade_payload(state: str = 'confirmed', amount_cents: int = 10000) -> dict:
    return {
        'id': 'aaaa1111-2222-3333-4444-bbbbccccdddd',
        'order': 'ox123_abcdef',
        'merchant': MERCHANT,
        'state': state,
        'amount_cents': amount_cents,
        'amount_currency': 'RUB',
        'rate': 88.23,
        'payment_data': {'url': 'https://pay.example.com/x'},
    }


def _webhook_sig(body: bytes) -> str:
    return _hmac_hex(body, WEBHOOK_SECRET)


def test_webhook_signature_valid() -> None:
    service = OplateXService()
    body = json.dumps(_trade_payload()).encode('utf-8')

    assert service.verify_webhook_signature(body, _webhook_sig(body)) is True


def test_webhook_signature_is_case_insensitive() -> None:
    service = OplateXService()
    body = json.dumps(_trade_payload()).encode('utf-8')

    assert service.verify_webhook_signature(body, _webhook_sig(body).upper()) is True


def test_webhook_signature_canonical_json_on_pretty_printed_retry() -> None:
    """Повторные доставки OplateX приходят с другим форматированием (pretty JSON),
    но подпись остаётся от канонической компактной сериализации."""
    service = OplateXService()
    payload = _trade_payload()
    canonical = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    pretty_body = json.dumps(payload, indent=2, ensure_ascii=False).encode('utf-8')

    assert service.verify_webhook_signature(pretty_body, _webhook_sig(canonical)) is True


def test_webhook_signature_rejects_api_token_signature() -> None:
    """Вебхук подписывается отдельным секретом, подпись API-токеном не проходит."""
    service = OplateXService()
    body = json.dumps(_trade_payload()).encode('utf-8')

    assert service.verify_webhook_signature(body, _hmac_hex(body, SECRET)) is False


def test_webhook_secret_falls_back_to_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'OPLATEX_WEBHOOK_SECRET', None, raising=False)
    service = OplateXService()
    body = json.dumps(_trade_payload()).encode('utf-8')

    assert service.verify_webhook_signature(body, _hmac_hex(body, SECRET)) is True


def test_webhook_signature_invalid() -> None:
    service = OplateXService()
    body = json.dumps(_trade_payload()).encode('utf-8')

    assert service.verify_webhook_signature(body, 'f' * 64) is False


def test_webhook_signature_missing() -> None:
    service = OplateXService()
    body = json.dumps(_trade_payload()).encode('utf-8')

    assert service.verify_webhook_signature(body, '') is False


def test_webhook_signature_rejects_tampered_body() -> None:
    service = OplateXService()
    body = json.dumps(_trade_payload(amount_cents=10000)).encode('utf-8')
    signature = _webhook_sig(body)
    tampered = json.dumps(_trade_payload(amount_cents=99900)).encode('utf-8')

    assert service.verify_webhook_signature(tampered, signature) is False


def test_webhook_signature_rejects_when_secret_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'OPLATEX_SECRET_KEY', None, raising=False)
    monkeypatch.setattr(settings, 'OPLATEX_WEBHOOK_SECRET', None, raising=False)
    service = OplateXService()
    body = json.dumps(_trade_payload()).encode('utf-8')

    assert service.verify_webhook_signature(body, _webhook_sig(body)) is False


# --- status map ---


@pytest.mark.parametrize(
    ('state', 'expected_status', 'expected_paid'),
    [
        ('created', 'pending', False),
        ('builded', 'pending', False),
        ('pending', 'pending', False),
        ('processing', 'processing', False),
        ('confirmed', 'success', True),
        ('completed', 'success', True),
        ('success', 'success', True),
        ('cancelled', 'canceled', False),
        ('canceled', 'canceled', False),
        ('expired', 'expired', False),
        ('failed', 'failed', False),
        ('CONFIRMED', 'success', True),  # регистр не важен
        ('weird_future_state', 'pending', False),  # неизвестное -> pending, не fail
        (None, 'pending', False),
        ('', 'pending', False),
    ],
)
def test_map_oplatex_state(state, expected_status, expected_paid) -> None:
    assert map_oplatex_state(state) == (expected_status, expected_paid)


# --- webhook route ---


class DummyBot:
    pass


def _get_route(router, path: str, method: str = 'POST'):
    for route in router.routes:
        if getattr(route, 'path', '') == path and method in getattr(route, 'methods', set()):
            return route
    raise AssertionError(f'Route {path} with method {method} not found')


def _build_request(body: bytes, headers: dict[str, str] | None = None) -> Request:
    headers = headers or {}
    scope = {
        'type': 'http',
        'asgi': {'version': '3.0'},
        'method': 'POST',
        'path': '/oplatex',
        'headers': [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers.items()],
        'client': ('203.0.113.10', 12345),
    }

    async def receive() -> dict:
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


@pytest.mark.anyio
async def test_route_returns_200_on_valid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(_trade_payload()).encode('utf-8')

    payment_service = SimpleNamespace(process_oplatex_webhook=AsyncMock(return_value=True))

    async def fake_callback(svc, payload_arg, method):
        assert method == 'process_oplatex_webhook'
        return await svc.process_oplatex_webhook(None, payload_arg)

    monkeypatch.setattr('app.webserver.payments._process_payment_service_callback', fake_callback)

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/oplatex')

    response = await route.endpoint(_build_request(body, headers={'Signature': _webhook_sig(body)}))

    assert response.status_code == 200
    payment_service.process_oplatex_webhook.assert_awaited_once()


@pytest.mark.anyio
async def test_route_returns_403_on_invalid_signature() -> None:
    body = json.dumps(_trade_payload()).encode('utf-8')

    payment_service = SimpleNamespace(process_oplatex_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/oplatex')

    response = await route.endpoint(_build_request(body, headers={'Signature': 'f' * 64}))

    assert response.status_code == 403
    payment_service.process_oplatex_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_route_returns_403_when_signature_header_missing() -> None:
    body = json.dumps(_trade_payload()).encode('utf-8')

    payment_service = SimpleNamespace(process_oplatex_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/oplatex')

    response = await route.endpoint(_build_request(body))

    assert response.status_code == 403
    payment_service.process_oplatex_webhook.assert_not_awaited()


@pytest.mark.anyio
async def test_route_returns_400_on_invalid_json() -> None:
    body = b'not-json'

    payment_service = SimpleNamespace(process_oplatex_webhook=AsyncMock())

    router = create_payment_router(DummyBot(), payment_service)
    assert router is not None
    route = _get_route(router, '/oplatex')

    response = await route.endpoint(_build_request(body, headers={'Signature': _webhook_sig(body)}))

    assert response.status_code == 400
    payment_service.process_oplatex_webhook.assert_not_awaited()


# --- process_oplatex_webhook: amount guard + idempotency ---


class _Service(OplateXPaymentMixin):
    pass


def _payment(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=1,
        user_id=10,
        order_id='ox123_abcdef',
        oplatex_id='aaaa1111-2222-3333-4444-bbbbccccdddd',
        amount_kopeks=10000,
        currency='RUB',
        status='pending',
        is_paid=False,
        transaction_id=None,
        metadata_json={},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mock_crud(monkeypatch: pytest.MonkeyPatch, payment: SimpleNamespace) -> SimpleNamespace:
    import app.database.crud.oplatex as oplatex_crud

    mocks = SimpleNamespace(
        get_by_order_id=AsyncMock(return_value=payment),
        get_by_oplatex_id=AsyncMock(return_value=payment),
        get_for_update=AsyncMock(return_value=payment),
        update_status=AsyncMock(return_value=payment),
    )
    monkeypatch.setattr(oplatex_crud, 'get_oplatex_payment_by_order_id', mocks.get_by_order_id)
    monkeypatch.setattr(oplatex_crud, 'get_oplatex_payment_by_oplatex_id', mocks.get_by_oplatex_id)
    monkeypatch.setattr(oplatex_crud, 'get_oplatex_payment_by_id_for_update', mocks.get_for_update)
    monkeypatch.setattr(oplatex_crud, 'update_oplatex_payment_status', mocks.update_status)
    return mocks


@pytest.mark.anyio
async def test_webhook_amount_mismatch_blocks_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = _payment(amount_kopeks=10000)
    mocks = _mock_crud(monkeypatch, payment)

    service = _Service()
    finalize = AsyncMock()
    monkeypatch.setattr(service, '_finalize_oplatex_payment', finalize, raising=False)

    payload = _trade_payload(state='confirmed', amount_cents=99900)
    result = await service.process_oplatex_webhook(AsyncMock(), payload)

    assert result is False
    finalize.assert_not_awaited()
    mocks.update_status.assert_awaited_once()
    assert mocks.update_status.await_args.kwargs['status'] == 'amount_mismatch'
    assert mocks.update_status.await_args.kwargs['is_paid'] is False


@pytest.mark.anyio
async def test_webhook_currency_mismatch_blocks_finalization(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = _payment(currency='RUB')
    mocks = _mock_crud(monkeypatch, payment)

    service = _Service()
    finalize = AsyncMock()
    monkeypatch.setattr(service, '_finalize_oplatex_payment', finalize, raising=False)

    payload = _trade_payload(state='confirmed')
    payload['amount_currency'] = 'USD'
    result = await service.process_oplatex_webhook(AsyncMock(), payload)

    assert result is False
    finalize.assert_not_awaited()
    assert mocks.update_status.await_args.kwargs['status'] == 'amount_mismatch'


@pytest.mark.anyio
async def test_webhook_is_idempotent_for_already_paid_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = _payment(is_paid=True, status='success')
    mocks = _mock_crud(monkeypatch, payment)

    service = _Service()
    finalize = AsyncMock()
    monkeypatch.setattr(service, '_finalize_oplatex_payment', finalize, raising=False)

    result = await service.process_oplatex_webhook(AsyncMock(), _trade_payload(state='confirmed'))

    assert result is True
    finalize.assert_not_awaited()
    mocks.update_status.assert_not_awaited()


@pytest.mark.anyio
async def test_webhook_confirmed_payment_finalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = _payment()
    _mock_crud(monkeypatch, payment)

    service = _Service()
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(service, '_finalize_oplatex_payment', finalize, raising=False)

    db = AsyncMock()
    result = await service.process_oplatex_webhook(db, _trade_payload(state='confirmed'))

    assert result is True
    finalize.assert_awaited_once()
    assert payment.is_paid is True
    assert payment.status == 'success'
    db.flush.assert_awaited()


@pytest.mark.anyio
async def test_webhook_non_success_state_updates_status_only(monkeypatch: pytest.MonkeyPatch) -> None:
    payment = _payment()
    mocks = _mock_crud(monkeypatch, payment)

    service = _Service()
    finalize = AsyncMock()
    monkeypatch.setattr(service, '_finalize_oplatex_payment', finalize, raising=False)

    result = await service.process_oplatex_webhook(AsyncMock(), _trade_payload(state='cancelled'))

    assert result is True
    finalize.assert_not_awaited()
    assert mocks.update_status.await_args.kwargs['status'] == 'canceled'
    assert mocks.update_status.await_args.kwargs['is_paid'] is False


@pytest.mark.anyio
async def test_webhook_missing_required_fields_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    mocks = _mock_crud(monkeypatch, _payment())

    service = _Service()
    result = await service.process_oplatex_webhook(AsyncMock(), {'order': 'ox1_x'})

    assert result is False
    mocks.get_by_order_id.assert_not_awaited()
