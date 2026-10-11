"""Create the compact five-JSON Front A delivery from a verified fixed run."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

SAMPLE_COUNT = 300
CONTROL_COUNT = 60
FAULT_COUNT = 240
FAULT_TYPE_COUNT = 11


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build(dataset: Path, release: Path, model: str) -> dict[str, object]:
    private = dataset / "private"
    summary_path = private / "summary.json"
    summary = json.loads(summary_path.read_text())
    detector_rows = [
        json.loads(line)
        for line in (dataset / "detector-input.jsonl").read_text().splitlines()
        if line.strip()
    ]
    labels = json.loads((dataset / "labels.json").read_text())
    fixed_inputs_path = Path("configs/inputs/front-a-300.json")
    fixed_inputs = json.loads(fixed_inputs_path.read_text())
    ids = [f"generation-{index:03d}" for index in range(1, SAMPLE_COUNT + 1)]
    if (summary.get("complete") is not True or summary.get("recorded_executions") != SAMPLE_COUNT
            or summary.get("pending_executions") != 0 or len(detector_rows) != SAMPLE_COUNT
            or list(labels) != ids or [row.get("trajectory_id") for row in detector_rows] != ids):
        raise ValueError("Only a complete, ordered 300-case collection can be packaged.")
    if any(any(key in row for key in ("label", "fault_id", "expected_error", "success"))
           for row in detector_rows):
        raise ValueError("Detector input contains labels or oracle fields.")
    controls = sum(value is None for value in labels.values())
    fault_counts = Counter(
        f"{value['module']}_{value['error_type']}"
        for value in labels.values() if value is not None
    )
    if (controls != CONTROL_COUNT or sum(fault_counts.values()) != FAULT_COUNT
            or len(fault_counts) != FAULT_TYPE_COUNT):
        raise ValueError("Expected 60 benign controls and 240 faults across 11 types.")
    if any(count not in (21, 22) for count in fault_counts.values()):
        raise ValueError("Fault distribution must contain 21 or 22 cases per type.")
    executions = private / "fixed-executions"
    if any(not (executions / f"{case_id}.json").exists()
           or (executions / f"{case_id}.error.json").exists() for case_id in ids):
        raise ValueError("Collection has missing or rejected baseline executions.")

    release.mkdir(parents=True, exist_ok=True)
    detector_path = release / "detector.json"
    fronta_path = release / "fronta300.json"
    labels_path = release / "labels.json"
    config_path = release / "run-config.json"
    manifest_path = release / "manifest.json"
    write_json(detector_path, detector_rows)
    write_json(fronta_path, fixed_inputs)
    write_json(labels_path, labels)

    source_manifest = json.loads((private / "manifest.json").read_text())
    source_config = json.loads((private / "run-config.json").read_text())
    routing_counts = Counter(
        (entry.get("routing") or (entry.get("provider", {}).get("order") or ["unknown"])[0])
        for entry in source_manifest.get("provider_routing", {}).values()
    )
    generation = source_config.get("generation", {})
    if generation.get("model") != model:
        raise ValueError("Configured model does not match the requested dataset model.")
    exact_phases = source_config.get("tool_schema_adjustment_phases", [])
    exact_case_ids = sorted(
        case_id for case_id, entry in source_manifest.get("provider_routing", {}).items()
        if entry.get("tool_schema_adjustment")
    )
    config = {
        "command": [
            ".venv/bin/python", "-m", "scripts.run_qwen_exact_count",
        ],
        "working_directory": "scenario-emulator project root",
        "effective_max_parallel_for_resumes": 1,
        "planned_samples": SAMPLE_COUNT,
        "recorded_samples": SAMPLE_COUNT,
        "success_controls": CONTROL_COUNT,
        "injected_faults": FAULT_COUNT,
        "detector_input": "detector.json",
        "labels": "labels.json",
        "fixed_inputs": "fronta300.json",
        "generation": generation,
        "fault_seed": 42,
        "prompts": "versioned-local; unchanged across phases",
        "reasoning": source_config.get("reasoning", {"enabled": False}),
        "routing_phases": {
            "counts_by_route": dict(routing_counts),
            "fallbacks": False,
            "require_parameters": True,
            "resume_provider": "deepinfra/bf16",
            "tool_schema_adjustment": "Pending 24 cases used exact-count-v4-terminal-save-outcome: salvar_formulario schema required exactly six questions; a successful save ended the clean task even if the provider failed on a subsequent turn.",
            "adjusted_accepted_case_ids": exact_case_ids,
            "system_and_user_prompts_changed": False,
            "historical_phases_recorded_in_private_run_config": len(exact_phases),
        },
        "fault_distribution": dict(fault_counts),
        "inputs_sha256": sha256(fixed_inputs_path),
        "runtime_sha256_original_manifest_protocol": source_manifest["protocol"]["runtime_sha256"],
        "budget": {
            "provider_max_price_per_million_tokens": {"prompt": 0.1, "completion": 0.15},
            "ledger": "private/budget-usd1-20260919.json (not included in detector input)",
            "authorized_limit_usd": json.loads(
                (private / "budget-usd1-20260919.json").read_text()
            )["limit_usd"],
            "committed_upper_bound_usd_at_packaging": json.loads(
                (private / "budget-usd1-20260919.json").read_text()
            )["committed_upper_bound_usd"],
            "reported_cost_usd_at_packaging": json.loads(
                (private / "budget-usd1-20260919.json").read_text()
            )["reported_cost_usd"],
            "unsettled_calls_retain_reservations": True,
        },
        "labels_join_key": "trajectory_id",
        "clean_label": None,
    }
    write_json(config_path, config)

    delivery = {
        "detector_input": "detector.json",
        "detector_format": "json-array",
        "detector_fields": ["trajectory_id", "task_description", "environment", "steps"],
        "fixed_inputs": "fronta300.json",
        "labels": "labels.json",
        "run_config": "run-config.json",
        "sample_count": SAMPLE_COUNT,
        "clean_controls": CONTROL_COUNT,
        "injected_faults": FAULT_COUNT,
        "labels_join_key": "trajectory_id",
        "clean_label": None,
        "sha256": {
            name: sha256(release / name)
            for name in ("detector.json", "fronta300.json", "labels.json", "run-config.json")
        },
    }
    manifest = {
        "schema_version": source_manifest["schema_version"],
        "campaign": source_manifest["campaign"],
        "description": source_manifest["description"],
        "created_at": source_manifest["created_at"],
        "experiment_profile": source_manifest["experiment_profile"],
        "model": model,
        "baseline_source_dir": source_manifest.get("baseline_source_dir"),
        "augmentations_per_baseline": source_manifest["augmentations_per_baseline"],
        "planned_baselines": SAMPLE_COUNT,
        "planned_executions": SAMPLE_COUNT,
        "planned_success_controls": CONTROL_COUNT,
        "baseline_batch_size": source_manifest["baseline_batch_size"],
        "front_b_contract_fields": source_manifest["front_b_contract_fields"],
        "fault_ids": source_manifest["fault_ids"],
        "distribution": {
            "successful_controls": CONTROL_COUNT,
            "injected_faults": FAULT_COUNT,
            "fault_counts": dict(fault_counts),
        },
        "delivery": delivery,
    }
    write_json(manifest_path, manifest)
    return {
        "sample_count": len(detector_rows),
        "controls": controls,
        "faults": sum(fault_counts.values()),
        "fault_counts": dict(fault_counts),
        "files": {path.name: path.stat().st_size for path in sorted(release.glob("*.json"))},
        "delivery_hashes": delivery["sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.dataset, args.release, args.model), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
