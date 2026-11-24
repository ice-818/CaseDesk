"""Authentication DTOs; credentials never become agent state."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

Role = Literal["buyer", "service_agent", "operator"]


class LoginRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    password: SecretStr


class PublicUser(BaseModel):
    user_id: str
    role: Role
    merchant_id: str | None = None
    status: str = "active"


class LoginResponse(BaseModel):
    access_token: str = Field(repr=False)
    token_type: str = "bearer"
    expires_at: datetime
    expires_in: int
    user: PublicUser


@dataclass(frozen=True)
class AuthIdentity:
    user_id: str
    role: Role
    source: Literal["bearer", "admin_token", "dev_admin"] = "bearer"
    token_digest: str | None = field(default=None, repr=False)
    merchant_id: str | None = None
    expires_at: datetime | None = None

    def public(self) -> PublicUser:
        return PublicUser(user_id=self.user_id, role=self.role, merchant_id=self.merchant_id)
