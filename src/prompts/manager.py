from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog

from src.clients.langfuse.client import get_langfuse_client
from src.settings import settings

logger = structlog.get_logger(__name__)
_VARIABLE_RE = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class ResolvedPrompt:
    name: str
    content: str
    source: str
    version: int | None = None
    labels: tuple[str, ...] = ()
    langfuse_prompt: Any | None = None


def _render_local(name: str, template: str, variables: dict[str, Any]) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            missing.append(key)
            return match.group(0)
        return str(variables[key])

    rendered = _VARIABLE_RE.sub(replace, template)
    if missing:
        raise KeyError(f"Prompt {name!r}: variáveis ausentes: {sorted(set(missing))}")
    return rendered


def resolve_prompt(
    name: str,
    local_fallback: str,
    *,
    variables: dict[str, Any] | None = None,
) -> ResolvedPrompt:
    variables = variables or {}
    client = get_langfuse_client()
    if client is not None:
        try:
            remote = client.get_prompt(
                name,
                label=settings.LANGFUSE_PROMPT_LABEL,
                type="text",
                cache_ttl_seconds=settings.PROMPT_CACHE_TTL_SECONDS,
                fetch_timeout_seconds=settings.PROMPT_FETCH_TIMEOUT_SECONDS,
            )
            return ResolvedPrompt(
                name=name,
                content=remote.compile(**variables) if variables else remote.prompt,
                source="langfuse",
                version=remote.version,
                labels=tuple(remote.labels),
                langfuse_prompt=remote,
            )
        except Exception as exc:  # pragma: no cover - rede/serviço externo
            logger.warning(
                "prompt.langfuse_fallback",
                prompt_name=name,
                label=settings.LANGFUSE_PROMPT_LABEL,
                error=str(exc),
            )

    return ResolvedPrompt(
        name=name,
        content=_render_local(name, local_fallback, variables),
        source="local",
    )
