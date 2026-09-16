import json

import pytest

from scripts.audit_success_controls import deterministic_issues


def case(command, weights):
    return {
        "task_description": f"Gerar formulário conforme o comando do coordenador: {command} Resultado esperado pelo oráculo: comply.",
        "steps": [
            {
                "module_outputs": {
                    "action": json.dumps(
                        {
                            "arguments": {
                                "payload": {
                                    "questions": [
                                        {
                                            "weight": weight,
                                            "description": "Explique sua experiência.",
                                        }
                                        for weight in weights
                                    ]
                                }
                            }
                        }
                    )
                }
            }
        ],
    }


@pytest.mark.parametrize("noun", ["pergunta", "questão"])
def test_one_question_in_text_cannot_pass_as_many(noun):
    issues = deterministic_issues(case(f"Gere uma {noun} discursiva sobre SIEM.", [8, 10, 7]))
    assert any("exige 1 pergunta(s), mas há 3" in issue for issue in issues)


def test_range_is_not_misread_as_exact_count():
    assert (
        deterministic_issues(case("Crie de 5 a 10 perguntas discursivas.", [8, 10, 7, 9, 6])) == []
    )


def test_monotonic_weights_are_rejected():
    assert any(
        "monotônica" in issue
        for issue in deterministic_issues(case("Gere 3 perguntas discursivas.", [10, 9, 8]))
    )
