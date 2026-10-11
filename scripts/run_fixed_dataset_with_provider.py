"""Resume a fixed campaign with explicit routing, without changing its prompts."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from src.agents.generation import dataset_generation
from src.agents.model import build_model
from src.schemas.questionnaire.schema import QuestionnaireExecution
from src.services.dataset.fixed import validate_clean_baseline
from src.services.dataset.profile import load_dataset_campaign_profile
from src.services.dataset.service import ErrorRecoveryDatasetService, _safe_filename
from src.services.questionnaire.service import QuestionnaireService
from src.services.scenario.service import ScenarioService


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def routed_model(provider: str):
    """The only request difference is the authorized routing preference."""
    if dataset_generation.get() is None:
        raise ValueError("Routing requires a fixed campaign context.")
    model = build_model()
    model.extra_body = {
        **model.extra_body,
        "provider": {**model.extra_body["provider"], "order": [provider]},
    }
    return model


class AuditedQuestionnaireService(QuestionnaireService):
    def __init__(self, private: Path, provider: str, adapter_sha256: str):
        self.private = private
        self.provider = provider
        self.adapter_sha256 = adapter_sha256

    async def execute(self, *args, **kwargs):
        config = dataset_generation.get()
        assert config is not None
        result = await super().execute(*args, **kwargs)
        serialized = result.model_dump_json(indent=2) + "\n"
        write_json(
            self.private / "provider-executions" / f"{result.trajectory_id}.json",
            {
                "case_id": kwargs["fixed_input_id"],
                "trajectory_id": result.trajectory_id,
                "checkpoint_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                "generation": config.model_dump(mode="json"),
                "reasoning": {"enabled": False},
                "provider": {
                    "require_parameters": True,
                    "allow_fallbacks": False,
                    "order": [self.provider],
                },
                "adapter_sha256": self.adapter_sha256,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
        return result


def routing_for_accepted(private: Path) -> dict:
    """Bind the routing to the exact content of each approved checkpoint."""
    assignment = json.loads((private / "fault-assignment.json").read_text())
    records = {}
    for checkpoint in sorted((private / "fixed-executions").glob("*.json")):
        if checkpoint.name.endswith(".error.json"):
            continue
        execution = QuestionnaireExecution.model_validate_json(checkpoint.read_text())
        try:
            validate_clean_baseline(execution, control=assignment[checkpoint.stem] is None)
        except ValueError:
            continue
        digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        audit = private / "provider-executions" / f"{execution.trajectory_id}.json"
        if audit.exists():
            record = json.loads(audit.read_text())
            if record["checkpoint_sha256"] != digest or record["case_id"] != checkpoint.stem:
                raise ValueError(f"Routing audit mismatch: {checkpoint.name}")
            records[checkpoint.stem] = record
        else:
            records[checkpoint.stem] = {
                "trajectory_id": execution.trajectory_id,
                "checkpoint_sha256": digest,
                "provider": {"require_parameters": True, "allow_fallbacks": False},
                "routing": "original_openrouter_default",
            }
    return records


async def run(args) -> int:
    campaign = load_dataset_campaign_profile(args.campaign)
    if campaign.generation is None or campaign.fixed_inputs is None:
        raise ValueError("Use a campaign with fixed inputs and generation parameters.")
    campaign = campaign.model_copy(
        update={
            "generation": campaign.generation.model_copy(update={"model": args.model}),
            "output_dir": Path("outputs/front-a-300") / _safe_filename(args.model),
        }
    )
    destination = args.output_dir or campaign.output_dir
    private = destination / "private"
    private.mkdir(parents=True, exist_ok=True)
    adapter_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    config_path = private / "run-config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    phase = {
        "started_at": datetime.now(UTC).isoformat(),
        "command": [sys.executable, "-m", "scripts.run_fixed_dataset_with_provider", *sys.argv[1:]],
        "generation": campaign.generation.model_dump(mode="json"),
        "provider": {"require_parameters": True, "allow_fallbacks": False, "order": [args.provider]},
        "effective_max_parallel": args.max_parallel,
        "adapter_path": "scripts/run_fixed_dataset_with_provider.py",
        "adapter_sha256": adapter_hash,
    }
    config.setdefault("explicit_routing_phases", []).append(phase)
    write_json(config_path, config)
    service = ErrorRecoveryDatasetService(
        scenario_service_factory=lambda: ScenarioService(
            questionnaire_service=AuditedQuestionnaireService(private, args.provider, adapter_hash)
        )
    )
    with patch("src.services.questionnaire.service.build_model", lambda: routed_model(args.provider)):
        summary = await service.run(
            campaign, limit=args.limit, max_parallel=args.max_parallel, output_dir=args.output_dir
        )
    routing = routing_for_accepted(private)
    manifest_path = private / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["provider_routing"] = routing
    manifest["explicit_routing_phases"] = config["explicit_routing_phases"]
    write_json(manifest_path, manifest)
    config.update(
        {
            "provider_routing": routing,
            "recorded_executions": summary.recorded_executions,
            "pending_executions": summary.pending_executions,
            "collection_status": "complete" if summary.complete else "incomplete",
        }
    )
    phase["finished_at"] = datetime.now(UTC).isoformat()
    write_json(config_path, config)
    print(summary.model_dump_json(indent=2))
    return 0 if summary.complete else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", required=True, help="Exact OpenRouter endpoint slug, e.g. deepinfra/bf16.")
    parser.add_argument("--max-parallel", type=int, default=5)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    destination = args.output_dir or Path("outputs/front-a-300") / _safe_filename(args.model)
    private = destination / "private"
    private.mkdir(parents=True, exist_ok=True)
    with (private / "collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
