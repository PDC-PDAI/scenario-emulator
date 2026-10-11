import copy

import pytest
from agno.tools.function import Function

from scripts.run_qwen_exact_count import constrain_tools
from src.schemas.questionnaire.schema import QuestionnairePayload


def save_tool():
    def salvar_formulario(questionnaireId: str, payload: QuestionnairePayload):
        return {}
    return {'type': 'function', 'function': Function.from_callable(salvar_formulario).to_dict()}


def test_exact_count_only_changes_save_tool_and_preserves_nullable_description():
    original = [save_tool(), {'type': 'function', 'function': {'name': 'get_info_vaga'}}]
    before = copy.deepcopy(original)
    result = constrain_tools(original, 6)
    assert original == before
    assert result[1] == original[1]
    f = result[0]['function']
    questions = f['parameters']['properties']['payload']['properties']['questions']
    assert (questions['minItems'], questions['maxItems']) == (6, 6)
    assert f['strict'] is True
    item = questions['items']
    assert set(item['required']) == set(item['properties'])
    assert {'type': 'null'} in item['properties']['description']['anyOf']
    assert 'default' not in item['properties']['description']
    assert item['additionalProperties'] is False
    assert constrain_tools(original, 7)[0]['function']['parameters']['properties']['payload']['properties']['questions']['minItems'] == 7  # noqa: E501, PLR2004
    assert (questions['minItems'], questions['maxItems']) == (6, 6)


@pytest.mark.parametrize('count', [None, 0, 51])
def test_invalid_requested_count_is_rejected(count):
    with pytest.raises(ValueError):
        constrain_tools([save_tool()], count)
