from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.middlewares.movo_start import MovoStartMiddleware


@pytest.mark.asyncio
async def test_start_runs_custom_service_then_continues_core_handler() -> None:
    middleware = MovoStartMiddleware()
    handler = AsyncMock(return_value='core-result')
    event = SimpleNamespace(text='/start campaign', answer=AsyncMock())
    user = SimpleNamespace(subscriptions=[])

    with (
        patch('app.middlewares.movo_start.Message', SimpleNamespace, create=True),
        patch('app.middlewares.movo_start.movo_start_service.handle', new=AsyncMock()) as handle,
    ):
        result = await middleware(handler, event, {'db_user': user})

    handle.assert_awaited_once_with(event, user)
    handler.assert_awaited_once_with(event, {'db_user': user})
    assert result == 'core-result'
