from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from src.schemas.questionnaire.schema import QuestionnairePayload
from src.security.camel import (
    DataOrigin,
    PolicyDeniedError,
    ProtectedValue,
    UnsafeContentError,
)
from src.security.schemas import (
    ProfessionalContext,
    QuestionScore,
    SecurityAssessment,
    SecurityVerdict,
)

_SECURITY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(?:todas?\s+)?(?:as\s+)?(?:instru[cç][oõ]es|regras|mensagens|prompts?)",
        r"\bignore\s+(?:all|any|the)?\s*(?:previous|prior|system)\s*(?:instructions?|prompts?)",
        r"\b(?:system|assistant|developer)\s*:",
        r"\b(?:mude|troque|altere|assuma)\s+(?:seu|o)\s+(?:papel|fun[cç][aã]o|personagem)",
        r"\b(?:reveal|show|print|repeat|exfiltrate)\b.{0,60}\b(?:prompt|secret|canary|token)\b",
        r"\b(?:revele|mostre|imprima|repita|exfiltre)\b.{0,60}\b(?:prompt|segredo|can[aá]rio|token)\b",
        r"\b(?:output|sa[ií]da|schema|json)\b.{0,60}\b(?:override|substitua|altere|ignore|quebre)\b",
        r"\b(?:d[eê]|atribua|retorne|garanta)\b.{0,40}\bnota\s*(?:10|m[aá]xima)\b",
        r"\bprompt\s*injection\b",
    )
)

_POLICY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:cpf|rg|senha|password|cart[aã]o\s+de\s+cr[eé]dito)\b",
        r"\b(?:orienta[cç][aã]o\s+sexual|religi[aã]o|gravidez|estado\s+civil)\b",
        r"\b(?:conte[uú]do\s+sexual|pornograf|viol[eê]ncia|ilegal)\b",
    )
)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split())


def deterministic_security_findings(*values: str) -> list[str]:
    text = "\n".join(normalize_text(value) for value in values if value)
    findings: list[str] = []
    for pattern in (*_SECURITY_PATTERNS, *_POLICY_PATTERNS):
        if match := pattern.search(text):
            findings.append(match.group(0)[:200])
    return findings


def validate_cited_evidence(evidence: Iterable[str], sources: Iterable[str]) -> None:
    normalized_sources = [normalize_text(source) for source in sources]
    for item in evidence:
        normalized = normalize_text(item)
        if not normalized or not any(normalized in source for source in normalized_sources):
            raise PolicyDeniedError("Evidência do modelo não corresponde literalmente à entrada.")


def authorize_security_assessment(
    assessment: SecurityAssessment,
    *,
    sources: Iterable[str],
    deterministic_findings: list[str],
    canary: str,
) -> None:
    produced = "\n".join([assessment.rationale, *assessment.evidence])
    if canary in produced:
        raise UnsafeContentError("Canário interno apareceu na avaliação de segurança.")
    validate_cited_evidence(assessment.evidence, sources)
    if deterministic_findings or assessment.verdict is not SecurityVerdict.SAFE:
        raise UnsafeContentError("Entrada recusada pela política de segurança.")


def authorize_professional_context(
    context: ProtectedValue[ProfessionalContext],
    *,
    sources: Iterable[str],
    canary: str,
) -> None:
    if not context.provenance.passed_through(("security_gate", "context_extraction")):
        raise PolicyDeniedError("Contexto não passou pelo gate e extrator obrigatórios.")
    validate_cited_evidence(context.value.source_evidence, sources)
    produced = "\n".join(
        [
            context.value.role_title,
            context.value.seniority or "",
            *context.value.responsibilities,
            *context.value.competencies,
            *context.value.professional_constraints,
        ]
    )
    if canary in produced or deterministic_security_findings(produced):
        raise UnsafeContentError("Contexto profissional contém conteúdo não autorizado.")


def authorize_questionnaire(
    payload: ProtectedValue[QuestionnairePayload],
    *,
    canaries: Iterable[str],
) -> None:
    required = ("security_gate", "context_extraction", "questionnaire_generation")
    if not payload.provenance.passed_through(required):
        raise PolicyDeniedError("Questionário não passou pelo pipeline CaMeL obrigatório.")
    produced = payload.value.model_dump_json(exclude_none=True)
    if any(canary and canary in produced for canary in canaries):
        raise UnsafeContentError("Questionário contém canário interno.")
    if deterministic_security_findings(produced):
        raise UnsafeContentError("Questionário contém conteúdo proibido.")


def authorize_evaluation(
    scores: list[ProtectedValue[QuestionScore]],
    *,
    expected_count: int,
    canaries: Iterable[str],
) -> None:
    if len(scores) != expected_count:
        raise PolicyDeniedError("Nem todas as respostas foram pontuadas.")
    required = ("answer_extraction", "question_scoring")
    produced: list[str] = []
    for score in scores:
        if DataOrigin.UNTRUSTED_ANSWER not in score.provenance.origins:
            raise PolicyDeniedError("Pontuação sem proveniência da resposta original.")
        if not score.provenance.passed_through(required):
            raise PolicyDeniedError("Pontuação não passou pelos estágios obrigatórios.")
        produced.append(score.value.justificativa)
    rendered = "\n".join(produced)
    if any(canary and canary in rendered for canary in canaries):
        raise PolicyDeniedError("Canário apareceu em campo produzido pelo avaliador.")
