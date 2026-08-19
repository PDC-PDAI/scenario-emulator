from __future__ import annotations

from typing import Any

from src.clients.langfuse.client import get_langfuse_client


def fetch_trace_export(trace_id: str) -> dict[str, Any]:
    """Busca um trace completo, incluindo observações e scores embutidos."""
    client = get_langfuse_client()
    if client is None:
        raise RuntimeError(
            "Langfuse não configurado. Confira LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY e LANGFUSE_BASE_URL."
        )

    trace = client.api.trace.get(trace_id.strip())
    return trace.model_dump(mode="json", by_alias=True)
