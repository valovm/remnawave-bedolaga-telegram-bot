from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.movo_onboarding import movo_start


@pytest.mark.asyncio
async def test_movo_start_runs_custom_flow_before_regular_start() -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=200))
    state = object()
    db = object()
    user = object()

    with (
        patch('app.handlers.movo_onboarding.movo_start_service.handle', new=AsyncMock(return_value=True)) as handle,
        patch('app.handlers.movo_onboarding.cmd_start', new=AsyncMock()) as regular_start,
    ):
        await movo_start(message, state, db, db_user=user)

    handle.assert_awaited_once_with(message, user)
    regular_start.assert_awaited_once_with(message, state, db, db_user=user)


@pytest.mark.asyncio
async def test_regular_start_still_runs_when_custom_flow_fails() -> None:
    message = SimpleNamespace(from_user=SimpleNamespace(id=200))

    with (
        patch(
            'app.handlers.movo_onboarding.movo_start_service.handle',
            new=AsyncMock(side_effect=RuntimeError('test failure')),
        ),
        patch('app.handlers.movo_onboarding.cmd_start', new=AsyncMock()) as regular_start,
    ):
        await movo_start(message, object(), object(), db_user=None)

    regular_start.assert_awaited_once()
