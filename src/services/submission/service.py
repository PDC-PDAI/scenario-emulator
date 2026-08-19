from __future__ import annotations

from src.schemas.questionnaire.schema import Questionnaire
from src.schemas.submission.schema import (
    QuestionnaireSubmission,
    QuestionnaireSubmissionRequest,
)


class SubmissionService:
    def create(
        self,
        *,
        scenario_id: str,
        questionnaire: Questionnaire,
        request: QuestionnaireSubmissionRequest,
    ) -> QuestionnaireSubmission:
        answers_by_number = {answer.question_number: answer for answer in request.answers}
        if len(answers_by_number) != len(request.answers):
            raise ValueError("Cada pergunta pode ser respondida apenas uma vez.")

        valid_numbers = set(range(1, len(questionnaire.questions) + 1))
        unknown_numbers = sorted(set(answers_by_number) - valid_numbers)
        if unknown_numbers:
            raise ValueError(f"Perguntas inexistentes na resposta: {unknown_numbers}.")

        missing_required = [
            number
            for number, question in enumerate(questionnaire.questions, start=1)
            if question.required and number not in answers_by_number
        ]
        if missing_required:
            raise ValueError(f"Perguntas obrigatórias sem resposta: {missing_required}.")

        ordered_answers = [answers_by_number[number] for number in sorted(answers_by_number)]
        return QuestionnaireSubmission(
            scenario_id=scenario_id,
            questionnaire_id=questionnaire.questionnaire_id,
            respondent_reference=request.respondent_reference,
            answers=ordered_answers,
        )
