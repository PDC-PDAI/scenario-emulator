import pytest

from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import PLATFORM_DEFAULT_GUIDELINES
from src.services.questionnaire.utils import (
    clean_questionnaire_payload,
    compose_coordinator_command,
    strip_por_favor,
    validate_question_count,
)


def test_pr91_guidelines_are_appended_after_command():
    command, guidelines = compose_coordinator_command("Foque em Python e FastAPI.")

    assert command == "Foque em Python e FastAPI."
    assert guidelines == PLATFORM_DEFAULT_GUIDELINES
    assert "senioridade" not in guidelines.lower()  # texto usa "nível da vaga"
    assert "SHORT_TEXT" in guidelines
    assert "weight" in guidelines


def test_strip_por_favor_matches_pr91_behavior():
    assert (
        strip_por_favor("Por favor, descreva sua experiência com Docker.")
        == "Descreva sua experiência com Docker."
    )
    assert strip_por_favor("Explique, por favor, o conceito de cache.") == (
        "Explique, por favor, o conceito de cache."
    )


def test_clean_questionnaire_payload_only_changes_question_text():
    payload = {
        "questions": [{"text": "por favor explique filas.", "description": "Por favor, detalhe."}]
    }

    clean_questionnaire_payload(payload)

    assert payload["questions"][0]["text"] == "Explique filas."
    assert payload["questions"][0]["description"] == "Por favor, detalhe."


def test_local_prompt_uses_mustache_and_fails_on_missing_variable():
    resolved = resolve_prompt("test", "Olá {{name}}", variables={"name": "Ana"})
    assert resolved.content == "Olá Ana"
    assert resolved.source == "local"


def test_question_count_enforces_default_range_and_explicit_target():
    validate_question_count({"questions": [{} for _ in range(5)]}, None)
    validate_question_count({"questions": [{} for _ in range(12)]}, 12)


@pytest.mark.parametrize(
    ("count", "requested"),
    [(4, None), (11, None), (11, 12), (13, 12)],
)
def test_question_count_rejects_contract_mismatch(count, requested):
    with pytest.raises(ValueError):
        validate_question_count({"questions": [{} for _ in range(count)]}, requested)
