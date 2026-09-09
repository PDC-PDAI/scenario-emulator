from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.cli import _apply_profile, _parser
from src.schemas.agent_debug.schema import ErrorModule, ErrorType
from src.schemas.experiment.schema import ExperimentProfile, PipelineProfile, ResearchFront
from src.services.experiment.profile import load_experiment_profile
from src.services.scenario.service import ScenarioService

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "configs/fronts/error_recovery.yaml"


def test_error_recovery_profile_catalogs_supported_faults_and_checkpoints():
    profile = load_experiment_profile(PROFILE)
    assert profile.front is ResearchFront.ERROR_RECOVERY
    assert profile.pipeline.malicious_commands == 0
    assert profile.error_recovery.capture_checkpoints is True
    assert profile.error_recovery.replay_enabled is False
    assert {mode.target_module for mode in profile.error_recovery.fault_catalog} == {
        ErrorModule.PLANNING, ErrorModule.ACTION, ErrorModule.SYSTEM,
    }
    assert {mode.error_type for mode in profile.error_recovery.fault_catalog} == {
        ErrorType.CONSTRAINT_IGNORANCE, ErrorType.IMPOSSIBLE_ACTION, ErrorType.INEFFICIENT_PLAN,
        ErrorType.MISALIGNMENT, ErrorType.INVALID_ACTION, ErrorType.FORMAT_ERROR,
        ErrorType.PARAMETER_ERROR, ErrorType.STEP_LIMIT, ErrorType.TOOL_EXECUTION_ERROR,
        ErrorType.LLM_LIMIT, ErrorType.ENVIRONMENT_ERROR,
    }


def test_cli_defaults_to_front_a_outside_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = _parser().parse_args(["run", "--brief", "Backend Python", "--benign", "1"])
    profile = _apply_profile(args)
    assert profile.front is ResearchFront.ERROR_RECOVERY
    assert profile == load_experiment_profile(PROFILE)
    assert args.benign == 1
    assert args.malicious == 0
    assert args.output == Path("outputs/error_recovery/scenario.json")


@pytest.mark.parametrize("flag", ["--benign-responses", "--malicious-responses"])
def test_cli_has_no_security_response_flags(flag):
    with pytest.raises(SystemExit):
        _parser().parse_args(["run", "--brief", "Backend Python", flag, "1"])


def test_profile_rejects_security_front_and_replay():
    profile = load_experiment_profile(PROFILE).model_dump(mode="json")
    with pytest.raises(ValidationError):
        ExperimentProfile.model_validate({**profile, "front": "security"})
    profile["error_recovery"]["replay_enabled"] = True
    with pytest.raises(ValidationError, match="contrato HTTP de re-rollout"):
        ExperimentProfile.model_validate(profile)


def test_pipeline_rejects_adversarial_campaigns_and_evaluation_options():
    with pytest.raises(ValidationError):
        PipelineProfile(malicious_commands=1)
    with pytest.raises(ValidationError):
        PipelineProfile(questionnaire_evaluator=True)


@pytest.mark.asyncio
async def test_service_rejects_security_before_calling_models():
    with pytest.raises(ValueError, match="Only error_recovery"):
        await ScenarioService().run("Backend Python", research_front="security")
