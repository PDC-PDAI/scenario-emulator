import pytest

from src.schemas.questionnaire.schema import Questionnaire, QuestionnaireQuestion
from src.schemas.submission.schema import QuestionnaireSubmissionRequest
from src.services.submission.service import SubmissionService


def _questionnaire() -> Questionnaire:
    return Questionnaire(
        questionnaire_id="questionnaire-1",
        job_description_id="job-1",
        questions=[
            QuestionnaireQuestion(
                text="Explique sua experiência com Python.",
                type="LONG_TEXT",
                weight=7,
                required=True,
                rationale="Competência principal.",
            )
        ],
    )


def test_rejects_duplicate_answer_numbers():
    request = QuestionnaireSubmissionRequest(
        answers=[
            {"question_number": 1, "text": "Primeira resposta."},
            {"question_number": 1, "text": "Resposta duplicada."},
        ]
    )

    with pytest.raises(ValueError, match="apenas uma vez"):
        SubmissionService().create(
            scenario_id="scenario-1",
            questionnaire=_questionnaire(),
            request=request,
        )


def test_rejects_answer_for_unknown_question():
    request = QuestionnaireSubmissionRequest(
        answers=[{"question_number": 2, "text": "Pergunta inexistente."}]
    )

    with pytest.raises(ValueError, match=r"\[2\]"):
        SubmissionService().create(
            scenario_id="scenario-1",
            questionnaire=_questionnaire(),
            request=request,
        )
