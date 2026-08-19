from __future__ import annotations

import re
from typing import Any

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_PHONE_WITH_AREA_CODE = re.compile(
    r"(?<![A-Za-z0-9-])(?:\+?55\s*)?\(?\d{2}\)?\s*9?\d{4}[-\s]?\d{4}(?![A-Za-z0-9-])"
)
_LOCAL_MOBILE = re.compile(r"(?<![A-Za-z0-9-])9\d{4}[-\s]?\d{4}(?![A-Za-z0-9-])")
_SECRET = re.compile(r"\b(?:sk|pk)-(?:proj-|lf-)?[A-Za-z0-9_-]{8,}\b")


def redact_text(value: str) -> str:
    value = _EMAIL.sub("[EMAIL_REDACTED]", value)
    value = _CPF.sub("[CPF_REDACTED]", value)
    value = _PHONE_WITH_AREA_CODE.sub("[PHONE_REDACTED]", value)
    value = _LOCAL_MOBILE.sub("[PHONE_REDACTED]", value)
    return _SECRET.sub("[SECRET_REDACTED]", value)


def redact_for_trace(value: Any) -> Any:
    """Remove PII/segredos de estruturas antes que elas alcancem o Langfuse."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact_for_trace(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_for_trace(item) for item in value]
    if hasattr(value, "model_dump"):
        return redact_for_trace(value.model_dump(mode="json"))
    return value
