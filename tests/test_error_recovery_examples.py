from __future__ import annotations

import json
from pathlib import Path

from src.schemas.agent_debug.schema import AgentDebugTrajectory, ErrorModule

CASE_DIR = (
    Path(__file__).resolve().parent.parent
    / "examples"
    / "error_recovery"
    / "invalid_action"
)
_CRITICAL_STEP = 2


def test_invalid_action_example_matches_agent_debug_contract_and_oracle():
    trajectory = AgentDebugTrajectory.model_validate_json(
        (CASE_DIR / "trajectory.json").read_text(encoding="utf-8")
    )
    jsonl_trajectory = AgentDebugTrajectory.model_validate_json(
        (CASE_DIR / "agent-debug.jsonl").read_text(encoding="utf-8").strip()
    )
    manifest = json.loads((CASE_DIR / "manifest.json").read_text(encoding="utf-8"))
    expected = json.loads(
        (CASE_DIR / "expected-diagnosis.json").read_text(encoding="utf-8")
    )

    assert trajectory == jsonl_trajectory
    assert trajectory.success is False
    assert len(trajectory.steps) == _CRITICAL_STEP
    for step in trajectory.steps:
        raw_output = json.loads(step.raw_output)
        assert raw_output["planning"] == step.module_outputs[ErrorModule.PLANNING]
        assert raw_output["action"] == json.loads(
            step.module_outputs[ErrorModule.ACTION]
        )
    critical = trajectory.steps[manifest["ground_truth"]["critical_failure_step"] - 1]
    assert "salvar_formulario_v2" in critical.module_outputs[ErrorModule.ACTION]
    assert "TOOL_NOT_FOUND" in critical.env_response
    assert expected["critical_error"]["step"] == manifest["ground_truth"][
        "critical_failure_step"
    ]
    assert expected["critical_error"]["module"] == manifest["ground_truth"][
        "critical_failure_module"
    ]
    assert expected["critical_error"]["error_type"] == manifest["ground_truth"][
        "critical_failure_type"
    ]
    assert set(expected["expected_feedback"]) == {"target_step", "expected_effect"}
    assert expected["expected_feedback"]["target_step"] == _CRITICAL_STEP
    assert manifest["replay"]["performed"] is False
