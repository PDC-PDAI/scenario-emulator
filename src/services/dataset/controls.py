"""Strict validation of the trajectories used as clean controls."""

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
        raise ValueError("Tool response is not an object.")
    if value.get("error") or value.get("ok") is False:
        raise ValueError("Tool response contains an error.")
    return value


def validate_control(data: dict) -> None:
    """Require lookup + save with no retry/error; audit against the full messages."""
    AgentDebugTrajectory.model_validate(data)
    steps = data["steps"]
    if data["success"] is not True or [s["index"] for s in steps] != [1, 2]:
        raise ValueError("A control requires success and exactly lookup + save.")
    actions = [json.loads(s["module_outputs"]["action"]) for s in steps]
    names = ["get_info_vaga", "salvar_formulario"]
    if [a.get("tool") for a in actions] != names:
        raise ValueError("Unexpected tool sequence.")
    messages = data.get("messages", [])
    if any(m.get("error") for m in messages):
        raise ValueError("History contains an error.")
    calls = [c for m in messages for c in (m.get("tool_calls") or [])]
    responses = [m for m in messages if m["role"] == "tool"]
    if [c.get("name") for c in calls] != names:
        raise ValueError("Call history diverges from the steps.")
    if [r.get("name") for r in responses] != names:
        raise ValueError("Response history is incomplete.")
    for action, call, response in zip(actions, calls, responses, strict=True):
        if call["arguments"] != action["arguments"]:
            raise ValueError("Arguments diverge between messages and steps.")
        if not call.get("id") or call["id"] != response.get("tool_call_id"):
            raise ValueError("Response does not match the call.")
    job, saved = [parse_response(r["content"]) for r in responses]
    if job.get("id") != actions[0]["arguments"]["code"]:
        raise ValueError("Lookup returned a different job opening.")
    args = actions[1]["arguments"]
    payload = QuestionnairePayload.model_validate(args["payload"])
    if saved.get("ok") is not True or saved.get("questionnaireId") != args["questionnaireId"]:
        raise ValueError("Persistence not confirmed for the questionnaire.")
    if saved.get("totalQuestions") != len(payload.questions):
        raise ValueError("Persisted count diverges from the payload.")
    terminal = json.loads(steps[-1]["env_response"])
    if parse_response(terminal.get("result", terminal)) != saved:
        raise ValueError("Persistence confirmation diverges from the messages.")
