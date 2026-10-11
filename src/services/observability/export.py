from __future__ import annotations

from typing import Any

from src.clients.langfuse.client import get_langfuse_client


def fetch_trace_export(trace_id: str) -> dict[str, Any]:
    """Fetch a complete trace, including embedded observations and scores."""
    client = get_langfuse_client()
    if client is None:
        raise RuntimeError(
            "Langfuse is not configured. Check LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL."
        )

    trace = client.api.trace.get(trace_id.strip())
    return trace.model_dump(mode="json", by_alias=True)
