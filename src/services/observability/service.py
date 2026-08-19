from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from src.clients.langfuse.client import get_langfuse_client
from src.schemas.observability.schema import TraceNode
from src.utils.privacy import redact_for_trace


class NullObservation:
    trace_id: str | None = None
    id: str | None = None

    def update(self, **_: Any) -> NullObservation:
        return self

    def end(self) -> None:
        return None


def node_metadata(node: TraceNode, **extra: Any) -> dict[str, Any]:
    return redact_for_trace(
        {
            "node_id": node.node_id,
            "depends_on": node.depends_on,
            "locus": node.locus.value,
            **extra,
        }
    )


@contextmanager
def observation(
    node: TraceNode,
    *,
    as_type: str = "span",
    input: Any = None,
    output: Any = None,
    model: str | None = None,
    prompt: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    client = get_langfuse_client()
    if client is None:
        yield NullObservation()
        return

    kwargs: dict[str, Any] = {
        "name": node.name,
        "as_type": as_type,
        "input": redact_for_trace(input),
        "output": redact_for_trace(output),
        "metadata": node_metadata(node, **(metadata or {})),
    }
    if model:
        kwargs["model"] = model
    if prompt is not None:
        kwargs["prompt"] = prompt
    with client.start_as_current_observation(**kwargs) as span:
        yield span


@contextmanager
def trace_attributes(
    *, session_id: str, tags: list[str], metadata: dict[str, Any]
) -> Iterator[None]:
    if get_langfuse_client() is None:
        yield
        return
    from langfuse import propagate_attributes  # noqa: PLC0415

    with propagate_attributes(
        session_id=session_id,
        tags=tags,
        metadata=redact_for_trace(metadata),
    ):
        yield


def current_trace_id() -> str | None:
    client = get_langfuse_client()
    return client.get_current_trace_id() if client is not None else None


def emit_reasoning_summary(*, node_prefix: str, text: str | None, depends_on: list[str]) -> None:
    client = get_langfuse_client()
    if client is None or not text:
        return
    from src.schemas.observability.schema import NodeLocus  # noqa: PLC0415

    node = TraceNode(
        node_id=f"{node_prefix}.reasoning.final",
        locus=NodeLocus.SUB_GER,
        depends_on=depends_on,
        name="reasoning",
    )
    span = client.start_observation(
        name=node.name,
        as_type="span",
        input={
            "context_node_ids": node.depends_on,
            "trigger": "output_schema_summary",
        },
        output=redact_for_trace(text[:4096]),
        metadata=node_metadata(node, source="output_schema_summary"),
    )
    span.end()
