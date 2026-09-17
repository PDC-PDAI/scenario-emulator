from __future__ import annotations

from typing import Any

from agno.models.openai import OpenAIChat, OpenAIResponses

from src.agents.generation import dataset_generation
from src.agents.openrouter import OpenRouterChat
from src.settings import settings


def _model_config() -> tuple[str, str]:
    if config := dataset_generation.get():
        return "openai_like", config.model
    provider = settings.LLM_PROVIDER
    model = settings.OLLAMA_MODEL if provider in {"ollama", "ceia"} else settings.OPENAI_MODEL
    return provider, model


def build_model() -> Any:
    if config := dataset_generation.get():
        api_key = settings.OPENROUTER_API_KEY
        if not api_key and settings.OPENAI_BASE_URL == config.base_url:
            api_key = settings.OPENAI_API_KEY
        if not api_key:
            raise ValueError("Configure OPENROUTER_API_KEY para executar a campanha fixa.")
        return OpenRouterChat(
            id=config.model,
            api_key=api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            top_p=config.top_p,
            max_tokens=config.max_tokens,
            max_retries=config.max_retries,
            timeout=config.timeout,
            extra_body={
                "reasoning": {"enabled": False},
                "provider": {"require_parameters": True, "allow_fallbacks": False},
            },
        )
    provider, model_id = _model_config()
    if provider in {"openai", "openai_like"}:
        is_openrouter = bool(
            settings.OPENAI_BASE_URL and "openrouter.ai" in settings.OPENAI_BASE_URL.lower()
        )
        model_class = OpenRouterChat if is_openrouter else OpenAIChat
        openrouter_reasoning = None
        if is_openrouter:
            openrouter_reasoning = {"exclude": False}
            if settings.OPENAI_REASONING_EFFORT:
                openrouter_reasoning["effort"] = settings.OPENAI_REASONING_EFFORT
            else:
                openrouter_reasoning["enabled"] = True
        return model_class(
            id=model_id,
            api_key=settings.OPENAI_API_KEY or None,
            base_url=settings.OPENAI_BASE_URL or None,
            max_retries=settings.OPENAI_MAX_RETRIES,
            reasoning_effort=(None if is_openrouter else settings.OPENAI_REASONING_EFFORT),
            extra_body={"reasoning": openrouter_reasoning} if openrouter_reasoning else None,
        )
    if provider == "openai_responses":
        return OpenAIResponses(
            id=model_id,
            api_key=settings.OPENAI_API_KEY or None,
            base_url=settings.OPENAI_BASE_URL or None,
            max_retries=settings.OPENAI_MAX_RETRIES,
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


def configured_model_identifier() -> str:
    return _model_config()[1]
