import io
import json
import logging

from app.core.constants import PII_MASK
from app.core.security import redact_mapping, redact_text
from app.observability.logging import ContextFilter, RedactingFormatter


def test_nested_structures_and_bearer_text_are_redacted():
    raw = {
        "PASSWORD": "sentinel-password",
        "nested": [{"token": "sentinel-token"}, ({"authorization": "Bearer sentinel-auth"},)],
        "message": "Bearer sentinel-bearer",
    }
    result = str(redact_mapping(raw))
    for secret in ("sentinel-password", "sentinel-token", "sentinel-auth", "sentinel-bearer"):
        assert secret not in result
    assert PII_MASK in result
    assert "sentinel" not in redact_text('password="sentinel" token=sentinel')


def test_formatter_parameters_extras_exceptions_and_access_query_are_redacted():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.getLogger("auth-redaction-test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("password=%s user=%s", "sentinel multi word;secret\nrest", "buyer")
        logger.info("authorization: %s", "Bearer sentinel-positional")
        logger.info(
            "payload %s",
            {"password": "sentinel-password", "items": [{"token": "sentinel-token"}]},
            extra={"authorization": "Bearer sentinel-extra"},
        )
        try:
            raise ValueError("password=sentinel-exception")
        except ValueError:
            logger.exception("authentication failure")
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "",
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1", "GET", "/api/v1/auth/me?token=sentinel-query", "1.1", 401),
            None,
        )
        handler.handle(record)
        output = stream.getvalue()
        assert "sentinel" not in output
        assert "/api/v1/auth/me" in output
        for line in output.splitlines():
            assert isinstance(json.loads(line), dict)
    finally:
        logger.removeHandler(handler)
