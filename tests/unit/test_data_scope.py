import asyncio
from dataclasses import FrozenInstanceError

import pytest

from app.core.data_scope import require_data_scope, scope_from_identity
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.schemas.auth import AuthIdentity
from tests.merchant_support import actor


def test_missing_scope_and_unbound_agent_fail_closed():
    with pytest.raises(UnauthorizedError):
        require_data_scope()
    with pytest.raises(ForbiddenError):
        scope_from_identity(AuthIdentity("a", "service_agent"))


@pytest.mark.parametrize("role", ["buyer", "operator"])
def test_null_merchant_is_platform_for_valid_platform_roles(role):
    with actor("u", role, None) as scope:
        assert scope.mode == "platform" and scope.merchant_id is None


def test_immutable_scope_resets_without_credentials():
    with actor() as scope:
        with pytest.raises(FrozenInstanceError):
            scope.merchant_id = "bad"
        assert len(scope.scope_key) == 64
    with pytest.raises(UnauthorizedError):
        require_data_scope()


@pytest.mark.asyncio
async def test_concurrent_scopes_do_not_leak():
    async def worker(merchant):
        with actor(merchant, merchant=merchant) as scope:
            await asyncio.sleep(0)
            assert require_data_scope() == scope
            return scope.scope_key

    a, b = await asyncio.gather(worker("m_demo"), worker("m_second"))
    assert a != b
