"""Short-lived authentication transactions, independent of chat snapshots."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.models import AuthSessionRow, UserRow


class AuthRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def user(self, user_id: str) -> UserRow | None:
        return await self.db.get(UserRow, user_id)

    async def session_user(self, digest: str):
        result = await self.db.execute(
            select(AuthSessionRow, UserRow)
            .join(UserRow, UserRow.user_id == AuthSessionRow.user_id)
            .where(AuthSessionRow.token_digest == digest)
        )
        return result.one_or_none()

    async def revoke(self, digest: str, now: datetime) -> None:
        await self.db.execute(
            update(AuthSessionRow)
            .where(AuthSessionRow.token_digest == digest, AuthSessionRow.revoked.is_(False))
            .values(revoked=True, revoked_at=now)
        )

    async def revoke_user(self, user_id: str, now: datetime) -> None:
        await self.db.execute(
            update(AuthSessionRow)
            .where(AuthSessionRow.user_id == user_id, AuthSessionRow.revoked.is_(False))
            .values(revoked=True, revoked_at=now)
        )
