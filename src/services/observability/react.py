"""Timeline ReAct ordenada, portada de rh-agent-agno commit 8dc3eab.

Cada turno emite ``reasoning -> action:<tool> -> observation`` em tempo real.
Quando o modelo não fornece summary de reasoning, registramos apenas a decisão
observável de escolher a tool, nunca chain-of-thought inventado.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agno.run.agent import (
    RunContentEvent,
    ToolCallCompletedEvent,
    ToolCallErrorEvent,
    ToolCallStartedEvent,
)

from src.clients.langfuse.client import get_langfuse_client
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.services.observability.service import node_metadata
from src.utils.privacy import redact_for_trace

_REASONING_LIMIT = 4096
_OBSERVATION_LIMIT = 2048
_LANGFUSE_TIMESTAMP_TICK_SECONDS = 0.001


class ReactSpanStreamer:
    def __init__(self, *, node_prefix: str, depends_on: list[str] | None = None) -> None:
        self._client = get_langfuse_client()
        self._node_prefix = node_prefix
        self._depends_on = depends_on or []
        self._open: dict[str, tuple[Any, str, str]] = {}
        self._reasoning_buffer = ""
        self._step = 0
        self._last_reasoning_id: str | None = None
        self.emitted_model_reasoning = False

    @property
    def terminal_dependencies(self) -> list[str]:
        return [self._last_reasoning_id] if self._last_reasoning_id else list(self._depends_on)

    @staticmethod
    def _tool_key(tool: Any, name: str) -> str:
        return str(getattr(tool, "tool_call_id", None) or name)

    async def handle(self, event: Any) -> None:
        if self._client is None:
            return
        if isinstance(event, RunContentEvent):
            delta = getattr(event, "reasoning_content", None)
            if delta:
                self._reasoning_buffer += str(delta)
        if isinstance(event, ToolCallStartedEvent):
            self._step += 1
            emitted = self._flush_reasoning(self._step)
            if not emitted:
                emitted = self._emit_tool_selection(event, self._step)
            if emitted:
                # O backend persiste milissegundos; evita empate visual com a action.
                await asyncio.sleep(_LANGFUSE_TIMESTAMP_TICK_SECONDS)
            self._on_started(event, self._step)
        elif isinstance(event, (ToolCallCompletedEvent, ToolCallErrorEvent)):
            self._on_finished(event)

    def flush_final_reasoning(self) -> None:
        self._flush_reasoning(self._step + 1)

    def _flush_reasoning(self, step: int) -> bool:
        text = self._reasoning_buffer.strip()
        self._reasoning_buffer = ""
        if self._client is None or not text:
            return False
        node = TraceNode(
            node_id=f"{self._node_prefix}.reasoning.{step:02d}",
            locus=NodeLocus.SUB_GER,
            depends_on=list(self._depends_on),
            name="reasoning",
        )
        span = self._client.start_observation(
            name=node.name,
            as_type="span",
            input={
                "step": step,
                "context_node_ids": node.depends_on,
                "trigger": "model_reasoning_summary",
            },
            output=redact_for_trace(text[:_REASONING_LIMIT]),
            metadata=node_metadata(node, source="model_summary"),
        )
        span.end()
        self._last_reasoning_id = node.node_id
        self.emitted_model_reasoning = True
        return True

    def _emit_tool_selection(self, event: ToolCallStartedEvent, step: int) -> bool:
        if self._client is None:
            return False
        tool_name = getattr(event.tool, "tool_name", None) or "tool"
        node = TraceNode(
            node_id=f"{self._node_prefix}.reasoning.{step:02d}",
            locus=NodeLocus.SUB_GER,
            depends_on=list(self._depends_on),
            name="reasoning",
        )
        span = self._client.start_observation(
            name=node.name,
            as_type="span",
            input={
                "step": step,
                "context_node_ids": node.depends_on,
                "trigger": "tool_selection",
            },
            output=f"Próximo passo selecionado: executar a tool {tool_name}.",
            metadata=node_metadata(node, source="tool_selection"),
        )
        span.end()
        self._last_reasoning_id = node.node_id
        return True

    def _on_started(self, event: ToolCallStartedEvent, step: int) -> None:
        if self._client is None:
            return
        tool = event.tool
        name = getattr(tool, "tool_name", None) or "tool"
        reasoning_id = self._last_reasoning_id or f"{self._node_prefix}.reasoning.{step:02d}"
        action_id = f"{self._node_prefix}.action.{step:02d}"
        node = TraceNode(
            node_id=action_id,
            locus=NodeLocus.MCP,
            depends_on=[reasoning_id],
            name=f"action:{name}",
        )
        span = self._client.start_observation(
            name=node.name,
            as_type="tool",
            input=redact_for_trace(getattr(tool, "tool_args", None)),
            metadata=node_metadata(node, tool=name),
        )
        self._open[self._tool_key(tool, name)] = (span, name, action_id)

    @staticmethod
    def _serialize_result(result: Any) -> Any:
        if result is None:
            return None
        if isinstance(result, (dict, list)):
            return redact_for_trace(json.loads(json.dumps(result, default=str)))
        return redact_for_trace(str(result)[:_OBSERVATION_LIMIT])

    def _on_finished(self, event: Any) -> None:
        if self._client is None:
            return
        tool = event.tool
        name = getattr(tool, "tool_name", None) or "tool"
        error = getattr(event, "error", None) or getattr(tool, "tool_call_error", None)
        result = self._serialize_result(getattr(tool, "result", None))
        entry = self._open.pop(self._tool_key(tool, name), None)
        if entry is None:
            return
        action, _, action_id = entry
        action.update(output=result)
        if error:
            action.update(level="ERROR", status_message=str(error)[:_OBSERVATION_LIMIT])
        action.end()

        node = TraceNode(
            node_id=action_id.replace(".action.", ".observation."),
            locus=NodeLocus.MCP,
            depends_on=[action_id],
            name="observation",
        )
        observation = self._client.start_observation(
            name=node.name,
            as_type="span",
            input={"tool": name},
            output=result,
            metadata=node_metadata(node, tool=name),
        )
        if error:
            observation.update(level="ERROR", status_message=str(error)[:_OBSERVATION_LIMIT])
        observation.end()
        self._depends_on = [node.node_id]
        self._last_reasoning_id = None
