from __future__ import annotations

from typing import Any

from agno.models.openai import OpenAIChat, OpenAIResponses

from src.settings import settings


def build_model() -> Any:
    provider = settings.LLM_PROVIDER
    if provider in {"openai", "openai_like"}:
        return OpenAIChat(
            id=settings.OPENAI_MODEL,
            api_key=settings.OPENAI_API_KEY or None,
            base_url=settings.OPENAI_BASE_URL or None,
            reasoning_effort=settings.OPENAI_REASONING_EFFORT,
        )
    if provider == "openai_responses":
        return OpenAIResponses(
            id=settings.OPENAI_MODEL,
            api_key=settings.OPENAI_API_KEY or None,
            base_url=settings.OPENAI_BASE_URL or None,
            reasoning_effort=settings.OPENAI_REASONING_EFFORT,
            reasoning_summary="auto",
        )
    if provider in {"ollama", "ceia"}:
        from agno.models.ollama import Ollama  # noqa: PLC0415

        request_params = (
            {"think": settings.OLLAMA_ENABLE_THINKING}
            if "qwen3" in settings.OLLAMA_MODEL.lower()
            else None
        )
        return Ollama(
            id=settings.OLLAMA_MODEL,
            host=settings.OLLAMA_BASE_URL,
            request_params=request_params,
        )
    raise ValueError(f"Provider não suportado: {provider}")


def get_model_identifier(model: Any) -> str:
    return str(getattr(model, "id", model))


def configured_model_identifier() -> str:
    if settings.LLM_PROVIDER in {"ollama", "ceia"}:
        return settings.OLLAMA_MODEL
    return settings.OPENAI_MODEL
