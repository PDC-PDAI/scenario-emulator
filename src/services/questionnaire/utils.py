from __future__ import annotations

import re
import unicodedata
from typing import Any

from langdetect import DetectorFactory, LangDetectException, detect

from src.prompts.raw_prompts import PLATFORM_DEFAULT_GUIDELINES

_POR_FAVOR_RE = re.compile(r"^\s*por\s+favor[,;:]?\s+", re.IGNORECASE)
_DEFAULT_MIN_QUESTIONS = 5
_DEFAULT_MAX_QUESTIONS = 10
_SHORT_LANGUAGE_TEXT_LENGTH = 30
_PORTUGUESE_MARKERS = (" como ", " para ", " que ", " você ", " sua ", " explique ", " descreva ")
DetectorFactory.seed = 0


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


def validate_questionnaire_content(payload: dict[str, Any]) -> None:
    """Aplica invariantes semânticas determinísticas antes do sink confiável."""
    normalized_questions: set[str] = set()
    for question in payload.get("questions", []):
        text = str(question.get("text", "")).strip()
        normalized = unicodedata.normalize("NFKC", text).casefold()
        normalized = re.sub(r"\W+", " ", normalized).strip()
        if normalized in normalized_questions:
            raise ValueError("O questionário contém perguntas duplicadas.")
        normalized_questions.add(normalized)
        if not is_probably_portuguese(text):
            raise ValueError("As perguntas devem ser escritas em português do Brasil.")
        description = question.get("description")
        if description and not is_probably_portuguese(str(description)):
            raise ValueError("As descrições devem ser escritas em português do Brasil.")


def is_probably_portuguese(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    lowered = f" {stripped.casefold()} "
    has_marker = any(marker in lowered for marker in _PORTUGUESE_MARKERS)
    if len(stripped) < _SHORT_LANGUAGE_TEXT_LENGTH:
        return has_marker or not re.search(r"\b(?:the|and|with|what|how|describe)\b", lowered)
    try:
        return detect(stripped) == "pt" or has_marker
    except LangDetectException:
        return has_marker
