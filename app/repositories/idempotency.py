"""Idempotency-key storage for write operations."""

import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_scope import require_data_scope
from app.core.error_codes import ErrorCode
from app.core.exceptions import AppError
from app.repositories.models import IdempotencyRow


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.scope = require_data_scope()

    def _key(self, key: str) -> str:
        self.scope.assert_current()
        return "idem:v2:" + hashlib.sha256(json.dumps([self.scope.scope_key, key], separators=(",", ":")).encode()).hexdigest()

    async def get_if_matches(self, key: str, fingerprint: str) -> dict | None:
        key = self._key(key)
        row = await self._session.get(IdempotencyRow, key)
        if row is None:
            return None
        if row.fingerprint != fingerprint:
            raise AppError(
                "idempotency key reused with different payload",
                error_code=ErrorCode.IDEMPOTENCY_CONFLICT,
                http_status=409,
            )
        return json.loads(row.response_json)

    async def put(self, key: str, fingerprint: str, response: dict) -> None:
        key = self._key(key)
        existing = await self._session.get(IdempotencyRow, key)
        if existing is not None:
            if existing.fingerprint != fingerprint:
                raise AppError(
                    "idempotency key reused with different payload",
                    error_code=ErrorCode.IDEMPOTENCY_CONFLICT,
                    http_status=409,
                )
            return
        self._session.add(
            IdempotencyRow(
                key=key,
                fingerprint=fingerprint,
                response_json=json.dumps(response, ensure_ascii=False),
            )
        )
