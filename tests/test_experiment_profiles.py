from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.cli import _apply_profile
from src.schemas.agent_debug.schema import ErrorModule, ErrorType
from src.schemas.experiment.schema import ResearchFront
from src.services.experiment.profile import load_experiment_profile

ROOT = Path(__file__).resolve().parent.parent
_CLI_BENIGN_OVERRIDE = 2


def test_security_profile_runs_evaluator_and_exports_all_artifacts():
    profile = load_experiment_profile(ROOT / "configs/fronts/security.yaml")

    assert profile.front is ResearchFront.SECURITY
    assert profile.pipeline.questionnaire_evaluator is True
    assert profile.pipeline.benign_responses + profile.pipeline.malicious_responses > 0
    assert profile.artifacts.scenario_path == Path("outputs/security/scenario.json")
    assert profile.artifacts.agent_debug_path == Path("outputs/security/agent-debug.jsonl")


def test_error_recovery_profile_catalogs_action_faults_and_checkpoints():
    profile = load_experiment_profile(ROOT / "configs/fronts/error_recovery.yaml")

    assert profile.front is ResearchFront.ERROR_RECOVERY
    assert profile.error_recovery is not None
    assert profile.error_recovery.capture_checkpoints is True
    assert profile.error_recovery.replay_enabled is False
    assert {mode.target_module for mode in profile.error_recovery.fault_catalog} == {
        ErrorModule.ACTION
    }
    assert ErrorType.INVALID_ACTION in {
        mode.error_type for mode in profile.error_recovery.fault_catalog
    }


def test_cli_profile_supplies_defaults_and_explicit_flags_win():
    args = argparse.Namespace(
        profile=ROOT / "configs/fronts/security.yaml",
        benign=_CLI_BENIGN_OVERRIDE,
        malicious=None,
        benign_responses=None,
        malicious_responses=0,
        output=None,
        jsonl=None,
        agent_debug_jsonl=None,
        trajectories_dir=None,
    )

    profile = _apply_profile(args)

    assert profile is not None
    assert args.benign == _CLI_BENIGN_OVERRIDE
    assert args.malicious == profile.pipeline.malicious_commands
    assert args.malicious_responses == 0
    assert args.output == Path("outputs/security/scenario.json")
    assert args.trajectories_dir == Path("outputs/security/trajectories")


def test_profile_rejects_replay_before_rollout_contract_exists(tmp_path):
    profile = tmp_path / "invalid.yaml"
    profile.write_text(
        """
schema_version: "1.0"
name: invalid_replay
front: error_recovery
description: Perfil inválido de teste.
pipeline:
  benign_commands: 1
  malicious_commands: 0
  benign_responses: 0
  malicious_responses: 0
  questionnaire_evaluator: false
artifacts:
  output_dir: outputs/test
error_recovery:
  capture_checkpoints: true
  replay_enabled: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="contrato HTTP de re-rollout"):
        load_experiment_profile(profile)
