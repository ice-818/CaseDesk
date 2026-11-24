"""Log redaction and PII masking. Never log secrets or raw identifiers."""

from __future__ import annotations

import re

from app.core.constants import PII_MASK

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "password",
        "cookie",
        "secret",
        "token",
        "token_digest",
        "password_hash",
        "x-admin-token",
    }
)

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_ID_CARD = re.compile(r"\b(\d{6})\d{8,10}(\d{2}[\dXx])\b")
_BANK_CARD = re.compile(r"\b(\d{4})\d{8,11}(\d{4})\b")
_ORDER_ID = re.compile(r"\b(ORD)[-_]?(\d{4})\d+(\d{2})\b", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',;\\]+")
_OPAQUE = re.compile(r"\bcds_[A-Za-z0-9_-]+")
_CREDENTIAL = re.compile(
    r'''(?i)((?:password(?:_hash)?|token(?:_digest)?|access_token|refresh_token|authorization|x-admin-token|api_key)["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[\s\S]+)'''
)


def redact_text(text: str) -> str:
    text = _BEARER.sub("Bearer " + PII_MASK, text)
    text = _OPAQUE.sub(PII_MASK, text)
    return mask_pii(_CREDENTIAL.sub(lambda match: match.group(1) + '"' + PII_MASK + '"', text))


def redact_value(value: object, depth: int = 0) -> object:
    if depth > 12:
        return PII_MASK
    if isinstance(value, dict):
        return {key: PII_MASK if str(key).lower() in _SECRET_KEYS else redact_value(item, depth + 1)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, depth + 1) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, BaseException):
        return redact_text(str(value))
    return value


def redact_mapping(payload: dict[str, object]) -> dict[str, object]:
    from typing import cast
    return cast(dict[str, object], redact_value(payload))


def mask_pii(text: str) -> str:
    text = _PHONE.sub(rf"\1{PII_MASK}\2", text)
    text = _ID_CARD.sub(rf"\1{PII_MASK}\2", text)
    text = _BANK_CARD.sub(rf"\1{PII_MASK}\2", text)
    text = _ORDER_ID.sub(rf"\1{PII_MASK}\3", text)
    return text
