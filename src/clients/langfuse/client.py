from __future__ import annotations

from functools import lru_cache
from typing import Any

import structlog

from src.settings import settings

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def get_langfuse_client() -> Any | None:
    """Return the global client only when every credential is present."""
    if not settings.langfuse_configured:
        return None
    try:
        # Late import: src.settings has already loaded .env before the SDK.
        from langfuse import get_client  # noqa: PLC0415

        return get_client()
    except Exception as exc:  # pragma: no cover - depends on the SDK/environment
        logger.warning("langfuse.client_unavailable", error=str(exc))
        return None


def flush_langfuse() -> None:
    client = get_langfuse_client()
    if client is not None:
        client.flush()
