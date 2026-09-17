"""Validação estrita das trajetórias usadas como controles corretos."""

from __future__ import annotations

import ast
import json

from src.schemas.agent_debug.schema import AgentDebugTrajectory
from src.schemas.questionnaire.schema import QuestionnairePayload


def parse_response(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = ast.literal_eval(value)
    if not isinstance(value, dict):
        raise ValueError("Resposta de tool não é objeto.")
    if value.get("error") or value.get("ok") is False:
        raise ValueError("Resposta de tool contém erro.")
    return value


def validate_control(data: dict) -> None:
    """Exige lookup + save, sem retry/erro; consulta messages completos para auditoria."""
    AgentDebugTrajectory.model_validate(data)
    steps = data["steps"]
    if data["success"] is not True or [s["index"] for s in steps] != [1, 2]:
        raise ValueError("Controle exige sucesso e exatamente lookup + save.")
    actions = [json.loads(s["module_outputs"]["action"]) for s in steps]
    names = ["get_info_vaga", "salvar_formulario"]
    if [a.get("tool") for a in actions] != names:
        raise ValueError("Sequência de tools inesperada.")
    messages = data.get("messages", [])
    if any(m.get("error") for m in messages):
        raise ValueError("Histórico contém erro.")
    calls = [c for m in messages for c in (m.get("tool_calls") or [])]
    responses = [m for m in messages if m["role"] == "tool"]
    if [c.get("name") for c in calls] != names:
        raise ValueError("Histórico de chamadas diverge dos steps.")
    if [r.get("name") for r in responses] != names:
        raise ValueError("Histórico de respostas incompleto.")
    for action, call, response in zip(actions, calls, responses, strict=True):
        if call["arguments"] != action["arguments"]:
            raise ValueError("Argumentos divergem entre messages e steps.")
        if not call.get("id") or call["id"] != response.get("tool_call_id"):
            raise ValueError("Resposta não corresponde à chamada.")
    job, saved = [parse_response(r["content"]) for r in responses]
    if job.get("id") != actions[0]["arguments"]["code"]:
        raise ValueError("Lookup retornou outra vaga.")
    args = actions[1]["arguments"]
    payload = QuestionnairePayload.model_validate(args["payload"])
    if saved.get("ok") is not True or saved.get("questionnaireId") != args["questionnaireId"]:
        raise ValueError("Persistência não confirmada para o formulário.")
    if saved.get("totalQuestions") != len(payload.questions):
        raise ValueError("Contagem persistida diverge do payload.")
    terminal = json.loads(steps[-1]["env_response"])
    if parse_response(terminal.get("result", terminal)) != saved:
        raise ValueError("Confirmação de persistência diverge dos messages.")
