from __future__ import annotations

from typing import Any, Literal

from agno.models.openai import OpenAIChat, OpenAIResponses

from src.settings import settings

ModelRole = Literal["default", "response_generator", "evaluator"]


def _role_config(role: ModelRole) -> tuple[str, str]:
    if role == "response_generator":
        provider = settings.RESPONSE_GENERATOR_LLM_PROVIDER or settings.LLM_PROVIDER
        override = settings.RESPONSE_GENERATOR_MODEL
    elif role == "evaluator":
        provider = settings.EVALUATOR_LLM_PROVIDER or settings.LLM_PROVIDER
        override = settings.EVALUATOR_MODEL
    else:
        provider = settings.LLM_PROVIDER
        override = None
    default_model = (
        settings.OLLAMA_MODEL if provider in {"ollama", "ceia"} else settings.OPENAI_MODEL
    )
    return provider, override or default_model


def build_model(role: ModelRole = "default") -> Any:
    provider, model_id = _role_config(role)
    if provider in {"openai", "openai_like"}:
        return OpenAIChat(
            id=model_id,
            api_key=settings.OPENAI_API_KEY or None,
            base_url=settings.OPENAI_BASE_URL or None,
            reasoning_effort=settings.OPENAI_REASONING_EFFORT,
        )
    if provider == "openai_responses":
        return OpenAIResponses(
            id=model_id,
            api_key=settings.OPENAI_API_KEY or None,
            base_url=settings.OPENAI_BASE_URL or None,
            reasoning_effort=settings.OPENAI_REASONING_EFFORT,
            reasoning_summary="auto",
        )
    if provider in {"ollama", "ceia"}:
        from agno.models.ollama import Ollama  # noqa: PLC0415

        request_params = (
            {"think": settings.OLLAMA_ENABLE_THINKING} if "qwen3" in model_id.lower() else None
        )
        return Ollama(
            id=model_id,
            host=settings.OLLAMA_BASE_URL,
            request_params=request_params,
        )
    raise ValueError(f"Provider não suportado: {provider}")


def get_model_identifier(model: Any) -> str:
    return str(getattr(model, "id", model))


def configured_model_identifier(role: ModelRole = "default") -> str:
    return _role_config(role)[1]
