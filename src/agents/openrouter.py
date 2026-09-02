from __future__ import annotations

from typing import Any

from agno.models.message import Message
from agno.models.openai import OpenAIChat


def _reasoning_details(payload: Any) -> list[dict[str, Any]]:
    details = getattr(payload, "reasoning_details", None)
    if details is None:
        model_extra = getattr(payload, "model_extra", None)
        if isinstance(model_extra, dict):
            details = model_extra.get("reasoning_details")
    if not isinstance(details, list):
        return []

    normalized: list[dict[str, Any]] = []
    for detail in details:
        normalized_detail = (
            detail.model_dump(exclude_none=True) if hasattr(detail, "model_dump") else detail
        )
        if isinstance(normalized_detail, dict):
            normalized.append(normalized_detail)
    return normalized


class OpenRouterChat(OpenAIChat):
    """OpenAI-compatible client that preserves reasoning blocks across tool calls."""

    def _format_message(
        self,
        message: Message,
        compress_tool_results: bool = False,
    ) -> dict[str, Any]:
        formatted = super()._format_message(message, compress_tool_results)
        provider_data = message.provider_data or {}
        details = provider_data.get("reasoning_details")
        if isinstance(details, list) and details:
            formatted["reasoning_details"] = details
        elif message.reasoning_content:
            formatted["reasoning"] = message.reasoning_content
        return formatted

    def _parse_provider_response(self, response: Any, response_format: Any = None) -> Any:
        model_response = super()._parse_provider_response(response, response_format)
        details = _reasoning_details(response.choices[0].message)
        if details:
            if model_response.provider_data is None:
                model_response.provider_data = {}
            model_response.provider_data["reasoning_details"] = details
        return model_response

    def _parse_provider_response_delta(self, response_delta: Any) -> Any:
        model_response = super()._parse_provider_response_delta(response_delta)
        if response_delta.choices:
            details = _reasoning_details(response_delta.choices[0].delta)
            if details:
                if model_response.provider_data is None:
                    model_response.provider_data = {}
                # Agno merges list-valued provider data in stream order. Keeping each
                # block unchanged is required when the assistant message is sent back
                # after a tool result.
                model_response.provider_data["reasoning_details"] = details
        return model_response
