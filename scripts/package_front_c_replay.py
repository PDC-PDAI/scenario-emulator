"""Build a label-free Front C replay input from a verified Front A delivery."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "outputs/front-a-300/google-gemma-4-31b-it"
PRIVATE = DATASET / "private"
RELEASE = DATASET / "drive-five-jsons"
SAMPLE_COUNT = 300


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def main() -> None:  # noqa: PLR0912, PLR0915 - build and audit one replay package
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--release", type=Path, default=RELEASE)
    parser.add_argument("--model", default="google/gemma-4-31b-it")
    args = parser.parse_args()
    dataset = args.dataset
    release = args.release
    private = dataset / "private"

    detector = json.loads((release / "detector.json").read_text())
    if (len(detector) != SAMPLE_COUNT
            or len({row["trajectory_id"] for row in detector}) != SAMPLE_COUNT):
        raise ValueError("detector.json must contain 300 unique case IDs")

    records = []
    for case in detector:
        case_id = case["trajectory_id"]
        if not case_id.startswith("generation-"):
            raise ValueError(f"Unexpected case ID: {case_id}")
        source_path = private / "fixed-executions" / f"{case_id}.json"
        execution = json.loads(source_path.read_text())
        source_trajectory = execution["agent_debug_trajectory"]
        messages = source_trajectory.get("messages")
        if not execution["benchmark_passed"] or not source_trajectory.get("success"):
            raise ValueError(f"Source execution is not a successful baseline: {case_id}")
        if not isinstance(messages, list) or not messages or messages[0].get("role") != "system":
            raise ValueError(f"Missing normalized conversation: {case_id}")

        assistant_tool_indices = [
            index for index, message in enumerate(messages)
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        source_steps = source_trajectory["steps"]
        if len(assistant_tool_indices) != len(source_steps):
            raise ValueError(f"Conversation/step alignment differs: {case_id}")

        questionnaire_ids = set()
        for message in messages:
            for call in message.get("tool_calls") or []:
                if call.get("name") != "salvar_formulario":
                    continue
                arguments = call.get("arguments") or {}
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                questionnaire_ids.add(arguments.get("questionnaireId"))
        if len(questionnaire_ids) != 1 or None in questionnaire_ids:
            raise ValueError(f"Expected one questionnaire ID in source messages: {case_id}")

        prefix_by_step = {
            step["index"]: assistant_tool_indices[position]
            for position, step in enumerate(source_steps)
        }
        # Synthetic later steps (for example step_limit) have no model turn in the
        # successful source conversation. Resume after the last tool result, before
        # the source assistant's final completion message.
        last_tool_turn = assistant_tool_indices[-1]
        for index in range(last_tool_turn + 1, len(messages)):
            if messages[index].get("role") == "assistant" and not messages[index].get("tool_calls"):
                last_tool_turn = index
                break

        max_step = max(step["index"] for step in case["steps"])
        resume_points = []
        for from_step in range(1, max_step + 1):
            if from_step in prefix_by_step:
                prefix_count = prefix_by_step[from_step]
            else:
                prefix_count = last_tool_turn
            resume_points.append({"from_step": from_step, "prefix_message_count": prefix_count})

        records.append({
            "case_id": case_id,
            "source_trajectory_id": source_trajectory["trajectory_id"],
            "job_opening_id": execution["coordinator_prompt"]["job_description_id"],
            "questionnaire_id": next(iter(questionnaire_ids)),
            "coordinator_prompt_id": execution["coordinator_prompt"]["id"],
            "trajectory": case,
            "messages": messages,
            "messages_sha256": hashlib.sha256(canonical_bytes(messages)).hexdigest(),
            "resume_points": resume_points,
        })

    payload = {
        "schema_version": "front-c-replay-input/1.0",
        "model": args.model,
        "records": records,
        "usage": {
            "join_key": "case_id (trajectory_id in detector.json)",
            "trajectory": "Injected, blind detector trajectory; use the diagnosis and feedback from Front B.",
            "messages": "Original normalized successful A conversation, retaining tool_call_id values.",
            "resume_points": "For B's from_step, take messages[:prefix_message_count], then apply C's feedback and resume logic.",
            "labels_included": False,
            "ground_truth_oracles_included": False,
            "source_run_config": "run-config.json",
        },
    }
    replay_path = release / "frontc-replay.json"
    write_json(replay_path, payload)

    config_path = release / "run-config.json"
    config = json.loads(config_path.read_text())
    config["front_c_replay"] = {
        "file": replay_path.name,
        "schema_version": payload["schema_version"],
        "sample_count": len(records),
        "join_key": "case_id",
        "resume_prefix_semantics": "messages[:prefix_message_count] for the from_step returned by B",
        "labels_included": False,
        "sha256": sha256(replay_path),
        "regeneration_command": (
            ".venv/bin/python -m scripts.package_front_c_replay"
            if dataset == DATASET and release == RELEASE
            else f".venv/bin/python -m scripts.package_front_c_replay --dataset {dataset} "
                 f"--release {release} --model {args.model}"
        ),
    }
    write_json(config_path, config)

    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    delivery = manifest.setdefault("delivery", {})
    delivery.setdefault("additional_files", {})[replay_path.name] = {
        "purpose": f"Front C re-execution context for {args.model}",
        "schema_version": payload["schema_version"],
        "sample_count": len(records),
        "join_key": "case_id",
        "labels_included": False,
        "sha256": sha256(replay_path),
    }
    delivery.setdefault("sha256", {})[config_path.name] = sha256(config_path)
    write_json(manifest_path, manifest)

    print(json.dumps({
        "records": len(records),
        "resume_points": sum(len(record["resume_points"]) for record in records),
        "file_bytes": replay_path.stat().st_size,
        "replay_sha256": sha256(replay_path),
        "config_sha256": sha256(config_path),
        "labels_included": False,
        "files": sorted(path.name for path in release.iterdir()),
    }, indent=2))


if __name__ == "__main__":
    main()
