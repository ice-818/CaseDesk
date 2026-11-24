"""Database-backed opaque credentials. All storage failures deny access."""

import asyncio
import hashlib
import logging
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import bcrypt
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.exceptions import (
    AppError,
    AuthenticationUnavailableError,
    ForbiddenError,
    InvalidRequestError,
    RateLimitError,
    UnauthorizedError,
)
from app.core.security import redact_text
from app.core.time import utcnow
from app.infra.rate_limit import InMemoryRateLimiter
from app.repositories.auth_repository import AuthRepository
from app.repositories.db import EXPECTED_REVISION
from app.repositories.models import AuthSessionRow, MerchantRow, UserRow
from app.schemas.auth import AuthIdentity, LoginResponse, Role

TOKEN_PATTERN = re.compile(r"^cds_[A-Za-z0-9_-]{43}$")
logger = logging.getLogger(__name__)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def token_digest(token: str) -> str:
    if not TOKEN_PATTERN.fullmatch(token):
        raise UnauthorizedError()
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def password_bytes(password: str) -> bytes:
    try:
        value = password.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidRequestError("password must be valid UTF-8") from None
    if not value or len(value) > 72:
        raise InvalidRequestError("password must contain 1-72 UTF-8 bytes")
    return value


async def hash_password(password: str) -> str:
    value = password_bytes(password)
    return (
        await asyncio.to_thread(lambda: bcrypt.hashpw(value, bcrypt.gensalt(rounds=12)))
    ).decode("ascii")


def auth_storage_error() -> AppError:
    return AuthenticationUnavailableError()


class AuthService:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.factory = factory
        self.settings = settings
        self.now = now
        self._dummy_hash: bytes | None = None
        self._hash_slots = asyncio.Semaphore(4)
        self._login_limiter = InMemoryRateLimiter(limit=settings.auth_login_limit_per_minute)

    async def check_schema(self, engine: AsyncEngine) -> None:
        try:
            async with engine.connect() as connection:
                names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
                if not {"users", "auth_session", "merchants", "alembic_version"}.issubset(names):
                    raise RuntimeError(
                        f"authentication migration {EXPECTED_REVISION} required"
                    )
                version = (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
                if version != EXPECTED_REVISION:
                    raise RuntimeError(
                        f"authentication migration {EXPECTED_REVISION} required"
                    )
        except SQLAlchemyError:
            raise auth_storage_error() from None

    async def _check_password(self, value: bytes, encoded: str | None) -> bool:
        async with self._hash_slots:
            if self._dummy_hash is None:
                self._dummy_hash = (await hash_password(secrets.token_urlsafe(24))).encode("ascii")
            expected = encoded.encode("ascii") if encoded else self._dummy_hash
            try:
                return await asyncio.to_thread(bcrypt.checkpw, value, expected)
            except (ValueError, UnicodeError):
                return False

    async def login(
        self, user_id: str, password: str, *, via="http", ip=None, user_agent=None
    ) -> LoginResponse:
        value = password_bytes(password)
        account_key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
        if not await self._login_limiter.allow("auth-account:" + account_key):
            raise RateLimitError()
        if ip and not await self._login_limiter.allow("auth-ip:" + ip):
            raise RateLimitError()
        try:
            async with self.factory() as db:
                user = await AuthRepository(db).user(user_id)
                encoded = user.password_hash if user else None
            valid = await self._check_password(value, encoded)
            async with self.factory() as db:
                # Serialize issuance with account administration on PostgreSQL.
                user = await db.scalar(
                    select(UserRow).where(UserRow.user_id == user_id).with_for_update()
                )
                if (
                    not valid
                    or user is None
                    or user.status != "active"
                    or user.password_hash != encoded
                ):
                    raise UnauthorizedError("invalid credentials")
                await self._validate_merchant(db, user)
                now = self.now()
                token = "cds_" + secrets.token_urlsafe(32)
                expiry = now + timedelta(seconds=self.settings.auth_session_ttl_seconds)
                db.add(
                    AuthSessionRow(
                        token_digest=token_digest(token),
                        user_id=user.user_id,
                        expires_at=expiry,
                        created_at=now,
                        created_via=via[:16],
                        created_ip=ip,
                        user_agent=redact_text(user_agent)[:512] if user_agent else None,
                    )
                )
                identity = AuthIdentity(
                    user.user_id, cast(Role, user.role), merchant_id=user.merchant_id
                )
                await db.commit()
                logger.info(
                    "auth_login_succeeded",
                    extra={"actor_user_id": user.user_id, "auth_source": "bearer"},
                )
                return LoginResponse(
                    access_token=token,
                    expires_at=expiry,
                    expires_in=self.settings.auth_session_ttl_seconds,
                    user=identity.public(),
                )
        except AppError:
            raise
        except Exception:
            raise auth_storage_error() from None

    async def authenticate(self, token: str) -> AuthIdentity:
        return await self._identity(token_digest(token))

    async def _identity(self, digest: str) -> AuthIdentity:
        try:
            async with self.factory() as db:
                result = await AuthRepository(db).session_user(digest)
                if result is None:
                    raise UnauthorizedError()
                session, user = result
                if (
                    session.revoked
                    or aware(session.expires_at) <= self.now()
                    or user.status != "active"
                ):
                    raise UnauthorizedError()
                await self._validate_merchant(db, user)
                return AuthIdentity(
                    user.user_id,
                    cast(Role, user.role),
                    token_digest=digest,
                    merchant_id=user.merchant_id,
                    expires_at=aware(session.expires_at),
                )
        except AppError:
            raise
        except Exception:
            raise auth_storage_error() from None

    async def _validate_merchant(self, db: AsyncSession, user: UserRow) -> None:
        if user.role == "service_agent":
            merchant = await db.get(MerchantRow, user.merchant_id) if user.merchant_id else None
            if merchant is None or merchant.status != "active":
                raise ForbiddenError("active merchant binding required")

    async def revalidate(self, identity: AuthIdentity) -> AuthIdentity:
        if identity.source != "bearer" or not identity.token_digest:
            raise UnauthorizedError()
        current = await self._identity(identity.token_digest)
        if current.user_id != identity.user_id:
            raise UnauthorizedError()
        if (current.role, current.merchant_id) != (identity.role, identity.merchant_id):
            raise ForbiddenError("identity scope changed")
        return current

    async def logout(self, identity: AuthIdentity) -> None:
        current = await self.revalidate(identity)
        if current.token_digest is None:
            raise UnauthorizedError()
        try:
            async with self.factory() as db:
                await AuthRepository(db).revoke(current.token_digest, self.now())
                await db.commit()
                logger.info(
                    "auth_logout",
                    extra={"actor_user_id": current.user_id, "auth_source": current.source},
                )
        except AppError:
            raise
        except Exception:
            raise auth_storage_error() from None

    async def disable_user(self, target_id: str, actor: AuthIdentity) -> None:
        if actor.source == "bearer":
            actor = await self.revalidate(actor)
        if actor.role != "operator":
            raise ForbiddenError()
        if actor.user_id == target_id:
            raise ForbiddenError("cannot disable yourself")
        try:
            async with self.factory() as db:
                # SQLite needs an explicit write transaction; FOR UPDATE alone
                # cannot serialize the last-active-operator check there.
                if db.get_bind().dialect.name == "sqlite":
                    await db.execute(text("BEGIN IMMEDIATE"))
                operators = list(
                    (
                        await db.scalars(
                            select(UserRow)
                            .where(UserRow.role == "operator", UserRow.status == "active")
                            .order_by(UserRow.user_id)
                            .with_for_update()
                        )
                    ).all()
                )
                target = await AuthRepository(db).user(target_id)
                if target is None:
                    raise AppError("user not found", http_status=404)
                if target.role == "operator" and target.status == "active" and len(operators) <= 1:
                    raise ForbiddenError("cannot disable the last active operator")
                target.status = "disabled"
                target.disabled_at = target.updated_at = self.now()
                await AuthRepository(db).revoke_user(target_id, self.now())
                await db.commit()
                logger.info(
                    "auth_user_disabled",
                    extra={
                        "actor_user_id": actor.user_id,
                        "target_user_id": target_id,
                        "auth_source": actor.source,
                    },
                )
        except AppError:
            raise
        except Exception:
            raise auth_storage_error() from None
