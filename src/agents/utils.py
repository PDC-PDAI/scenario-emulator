from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel

from src.schemas.agent_debug.schema import ChatMessage, ChatToolCall

T = TypeVar("T", bound=BaseModel)


def parse_model_output(content: Any, schema: type[T]) -> T:
    if isinstance(content, schema):
        return content
    if isinstance(content, BaseModel):
        return schema.model_validate(content.model_dump())
    if isinstance(content, dict):
        return schema.model_validate(content)
    if isinstance(content, str):
        cleaned = content.strip().removeprefix("```json").removesuffix("```").strip()
        return schema.model_validate(json.loads(cleaned))
    return schema.model_validate(content)


def extract_usage(response: Any) -> dict[str, int] | None:
    for raw_candidate in (getattr(response, "usage", None), getattr(response, "metrics", None)):
        if raw_candidate is None:
            continue
        candidate = raw_candidate
        if hasattr(raw_candidate, "model_dump"):
            candidate = raw_candidate.model_dump(exclude_none=True)
        elif hasattr(raw_candidate, "__dict__") and not isinstance(raw_candidate, dict):
            candidate = vars(raw_candidate)
        if not isinstance(candidate, dict):
            continue
        input_tokens = candidate.get("input_tokens") or candidate.get("prompt_tokens")
        output_tokens = candidate.get("output_tokens") or candidate.get("completion_tokens")
        usage: dict[str, int] = {}
        if input_tokens is not None:
            usage["input"] = int(input_tokens)
        if output_tokens is not None:
            usage["output"] = int(output_tokens)
        if usage:
            return usage
    return None


def _parse_tool_arguments(arguments: Any) -> Any:
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return arguments


def _chat_tool_calls(raw: Any) -> list[ChatToolCall] | None:
    if not raw:
        return None
    calls: list[ChatToolCall] = []
    for tool_call in raw:
        function = tool_call.get("function") or {}
        calls.append(
            ChatToolCall(
                id=tool_call.get("id"),
                name=function.get("name"),
                arguments=_parse_tool_arguments(function.get("arguments")),
            )
        )
    return calls


def clean_agent_messages(run_output: Any) -> list[ChatMessage] | None:
    """``RunOutput.messages`` do Agno → conversa crua no contrato exportável.

    Preserva a ordem exata e os ``tool_call_id`` reais — é o que permite à
    Frente C reexecutar com prefixo verbatim. Devolve ``None`` quando o run não
    expôs mensagens (execução legada ou stub), mantendo o campo ausente.
    """
    raw = getattr(run_output, "messages", None) or []
    messages: list[ChatMessage] = []
    for message in raw:
        data = message.to_dict() if hasattr(message, "to_dict") else message
        if not isinstance(data, dict) or not data.get("role"):
            continue
        role = str(data["role"])
        chat = ChatMessage(role=role, content=data.get("content"))
        if role == "assistant":
            chat.tool_calls = _chat_tool_calls(data.get("tool_calls"))
            reasoning = data.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                chat.reasoning = reasoning.strip()
        elif role == "tool":
            chat.tool_call_id = data.get("tool_call_id")
            chat.name = data.get("tool_name")
            if data.get("tool_call_error"):
                chat.error = True
        messages.append(chat)
    return messages or None
