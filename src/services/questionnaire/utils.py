from __future__ import annotations

import re
from typing import Any

from src.prompts.raw_prompts import PLATFORM_DEFAULT_GUIDELINES

_POR_FAVOR_RE = re.compile(r"^\s*por\s+favor[,;:]?\s+", re.IGNORECASE)
_DEFAULT_MIN_QUESTIONS = 5
_DEFAULT_MAX_QUESTIONS = 10


def compose_coordinator_command(command: str | None) -> tuple[str, str]:
    """Mantém comando e diretrizes em variáveis separadas para preservar a cerca XML."""
    return (command or "").strip(), PLATFORM_DEFAULT_GUIDELINES


def strip_por_favor(text: str) -> str:
    """Pós-processamento portado do PR #91 do rh-agent-agno."""
    cleaned = _POR_FAVOR_RE.sub("", text, count=1)
    if cleaned == text or not cleaned:
        return text
    return cleaned[0].upper() + cleaned[1:]


def clean_questionnaire_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for question in payload.get("questions", []):
        text = question.get("text")
        if isinstance(text, str):
            question["text"] = strip_por_favor(text)
    return payload


def validate_question_count(payload: dict[str, Any], requested_count: int | None) -> None:
    count = len(payload.get("questions", []))
    if requested_count is not None and count != requested_count:
        raise ValueError(
            f"Quantidade inválida: esperado exatamente {requested_count}, recebido {count}."
        )
    if requested_count is None and not _DEFAULT_MIN_QUESTIONS <= count <= _DEFAULT_MAX_QUESTIONS:
        raise ValueError(
            "Quantidade padrão inválida: esperado entre "
            f"{_DEFAULT_MIN_QUESTIONS} e {_DEFAULT_MAX_QUESTIONS}, recebido {count}."
        )
