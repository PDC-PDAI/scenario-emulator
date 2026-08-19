from __future__ import annotations

from functools import lru_cache
from typing import Any

import structlog

from src.settings import settings

logger = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def get_langfuse_client() -> Any | None:
    """Retorna o client global somente quando todas as credenciais estão presentes."""
    if not settings.langfuse_configured:
        return None
    try:
        # Import tardio: o .env já foi carregado em src.settings antes do SDK.
        from langfuse import get_client  # noqa: PLC0415

        return get_client()
    except Exception as exc:  # pragma: no cover - depende do SDK/ambiente
        logger.warning("langfuse.client_unavailable", error=str(exc))
        return None


def flush_langfuse() -> None:
    client = get_langfuse_client()
    if client is not None:
        client.flush()
