import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.run_fixed_dataset_with_provider import routed_model, routing_for_accepted
from src.agents.generation import DatasetGenerationConfig, generation_scope


def test_routing_preserves_generation_parameters_and_original_body():
    original_body = {
        "reasoning": {"enabled": False},
        "provider": {"require_parameters": True, "allow_fallbacks": False},
    }
    model = SimpleNamespace(
        id="qwen/qwen3.5-9b", temperature=0, top_p=1, max_tokens=8192,
        max_retries=2, timeout=120, extra_body=original_body,
    )
    parameters = {k: v for k, v in vars(model).items() if k != "extra_body"}
    with (
        generation_scope(DatasetGenerationConfig(model=model.id)),
        patch("scripts.run_fixed_dataset_with_provider.build_model", return_value=model),
    ):
        routed = routed_model("deepinfra/bf16")
    assert {k: v for k, v in vars(routed).items() if k != "extra_body"} == parameters
    assert routed.extra_body == {
        "reasoning": {"enabled": False},
        "provider": {
            "require_parameters": True, "allow_fallbacks": False, "order": ["deepinfra/bf16"],
        },
    }
    assert "order" not in original_body["provider"]


def test_routing_rejects_non_campaign_context():
    with pytest.raises(ValueError, match="fixed campaign"):
        routed_model("deepinfra/bf16")


def test_routing_audit_rejects_changed_checkpoint(tmp_path):
    (tmp_path / "fixed-executions").mkdir()
    (tmp_path / "provider-executions").mkdir()
    (tmp_path / "fault-assignment.json").write_text('{"generation-001": null}')
    checkpoint = tmp_path / "fixed-executions/generation-001.json"
    checkpoint.write_text("{}")
    audit = tmp_path / "provider-executions/trajectory-001.json"
    audit.write_text(json.dumps({
        "case_id": "generation-001", "checkpoint_sha256": hashlib.sha256(b"{}").hexdigest(),
        "provider": {"order": ["deepinfra/bf16"]},
    }))
    with (
        patch("scripts.run_fixed_dataset_with_provider.QuestionnaireExecution.model_validate_json",
              return_value=SimpleNamespace(trajectory_id="trajectory-001")),
        patch("scripts.run_fixed_dataset_with_provider.validate_clean_baseline"),
    ):
        assert routing_for_accepted(tmp_path)["generation-001"]["provider"]["order"] == ["deepinfra/bf16"]
        checkpoint.write_text('{"modified": true}')
        with pytest.raises(ValueError, match="Routing audit mismatch"):
            routing_for_accepted(tmp_path)
