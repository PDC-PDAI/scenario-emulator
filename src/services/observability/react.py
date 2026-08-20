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
from src.schemas.agent_debug.schema import AgentDebugTrajectoryStep, ErrorModule
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.services.observability.service import node_metadata
from src.utils.privacy import redact_for_trace

_REASONING_LIMIT = 4096
_OBSERVATION_LIMIT = 2048
_STEP_INPUT_LIMIT = 12_000
_LANGFUSE_TIMESTAMP_TICK_SECONDS = 0.001


class ReactSpanStreamer:
    def __init__(
        self,
        *,
        node_prefix: str,
        depends_on: list[str] | None = None,
        initial_input: str = "",
    ) -> None:
        self._client = get_langfuse_client()
        self._node_prefix = node_prefix
        self._depends_on = depends_on or []
        self._open: dict[str, tuple[Any, str, str]] = {}
        self._open_steps: dict[str, int] = {}
        self._steps: list[AgentDebugTrajectoryStep] = []
        self._environment_history: list[str] = []
        self._initial_input = initial_input
        self._reasoning_buffer = ""
        self._step = 0
        self._last_reasoning_id: str | None = None
        self.emitted_model_reasoning = False

    @property
    def terminal_dependencies(self) -> list[str]:
        return [self._last_reasoning_id] if self._last_reasoning_id else list(self._depends_on)

    @property
    def trajectory_steps(self) -> list[AgentDebugTrajectoryStep]:
        """Snapshot dos checkpoints capturados, independente do Langfuse."""
        return [step.model_copy(deep=True) for step in self._steps]

    @staticmethod
    def _tool_key(tool: Any, name: str) -> str:
        return str(getattr(tool, "tool_call_id", None) or name)

    async def handle(self, event: Any) -> None:
        if isinstance(event, RunContentEvent):
            delta = getattr(event, "reasoning_content", None)
            if delta:
                self._reasoning_buffer += str(delta)
        if isinstance(event, ToolCallStartedEvent):
            self._step += 1
            reasoning = self._consume_reasoning()
            self._capture_started(event, self._step, reasoning)
            if self._client is not None:
                emitted = self._emit_reasoning(reasoning, self._step)
                if not emitted:
                    emitted = self._emit_tool_selection(event, self._step)
                if emitted:
                    # O backend persiste milissegundos; evita empate visual com a action.
                    await asyncio.sleep(_LANGFUSE_TIMESTAMP_TICK_SECONDS)
                self._on_started(event, self._step)
        elif isinstance(event, (ToolCallCompletedEvent, ToolCallErrorEvent)):
            self._capture_finished(event)
            if self._client is not None:
                self._on_finished(event)

    def flush_final_reasoning(self) -> str:
        text = self._consume_reasoning()
        if self._client is not None:
            self._emit_reasoning(text, self._step + 1)
        return text

    def _consume_reasoning(self) -> str:
        text = self._reasoning_buffer.strip()
        self._reasoning_buffer = ""
        return text

    def _emit_reasoning(self, text: str, step: int) -> bool:
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

    def _current_step_input(self) -> str:
        parts = [self._initial_input.strip()]
        if self._environment_history:
            parts.append(
                "Histórico das respostas do ambiente:\n"
                + "\n".join(self._environment_history)
            )
        return "\n\n".join(part for part in parts if part)[-_STEP_INPUT_LIMIT:]

    @staticmethod
    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value[:_OBSERVATION_LIMIT]
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)[
            :_OBSERVATION_LIMIT
        ]

    def _capture_started(self, event: ToolCallStartedEvent, step: int, reasoning: str) -> None:
        tool = event.tool
        name = getattr(tool, "tool_name", None) or "tool"
        key = self._tool_key(tool, name)
        arguments = self._serialize_result(getattr(tool, "tool_args", None))
        planning = reasoning or f"Próximo passo selecionado: executar a tool {name}."
        action = self._as_text({"tool": name, "arguments": arguments})
        raw_output = self._as_text({"planning": planning, "action": action})
        self._steps.append(
            AgentDebugTrajectoryStep(
                index=step,
                module_outputs={
                    ErrorModule.PLANNING: planning,
                    ErrorModule.ACTION: action,
                },
                step_input=self._current_step_input(),
                raw_output=raw_output,
            )
        )
        self._open_steps[key] = len(self._steps) - 1

    def _capture_finished(self, event: Any) -> None:
        tool = event.tool
        name = getattr(tool, "tool_name", None) or "tool"
        key = self._tool_key(tool, name)
        step_position = self._open_steps.pop(key, None)
        if step_position is None:
            return
        error = getattr(event, "error", None) or getattr(tool, "tool_call_error", None)
        result = self._serialize_result(getattr(tool, "result", None))
        environment_payload = {"tool": name, "result": result}
        if error:
            environment_payload["error"] = str(error)[:_OBSERVATION_LIMIT]
        env_response = self._as_text(environment_payload)
        step = self._steps[step_position]
        self._steps[step_position] = step.model_copy(update={"env_response": env_response})
        self._environment_history.append(f"step {step.index}: {env_response}")

    def record_terminal_failure(
        self,
        *,
        code: str,
        message: str,
        final_output: Any = None,
        planning: str = "",
    ) -> None:
        """Registra falha sem tool terminal como um step analisável."""
        self._step += 1
        action = self._as_text(
            {
                "operation": "agent_response_without_terminal_tool",
                "output": self._serialize_result(final_output),
            }
        )
        plan = planning or "A execução terminou sem selecionar uma tool terminal válida."
        env_response = self._as_text({"error": code, "message": message})
        self._steps.append(
            AgentDebugTrajectoryStep(
                index=self._step,
                module_outputs={
                    ErrorModule.PLANNING: plan,
                    ErrorModule.ACTION: action,
                },
                step_input=self._current_step_input(),
                env_response=env_response,
                raw_output=self._as_text({"planning": plan, "action": action}),
            )
        )
        self._environment_history.append(f"step {self._step}: {env_response}")

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
