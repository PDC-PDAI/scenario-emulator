from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import TypeVar

from src.clients.langfuse.client import flush_langfuse
from src.schemas.experiment.schema import (
    ExperimentProfile,
    PipelineProfile,
    validate_front_pipeline,
)
from src.services.agent_debug.service import save_trajectory_files, scenario_trajectories
from src.services.experiment.profile import load_experiment_profile
from src.services.scenario.service import ScenarioService

_T = TypeVar("_T")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scenario-emulator",
        description="Generate Front A trajectories and controlled faults for AgentDebug-RH.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Generate a job opening, 1:N commands, and questionnaires.")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--brief", help="Text brief for the job opening.")
    source.add_argument("--brief-file", type=Path, help="UTF-8 file containing the brief.")
    run.add_argument(
        "--profile",
        type=Path,
        default=Path(__file__).parent / "profiles" / "error_recovery.yaml",
        help="Front A YAML profile; explicit flags override its values.",
    )
    run.add_argument("--benign", type=int, help="Number of benign commands.")
    run.add_argument(
        "--malicious", type=int, choices=[0], help="Front A only generates benign baselines."
    )
    run.add_argument("--output", type=Path, help="Full scenario JSON file.")
    run.add_argument(
        "--jsonl",
        type=Path,
        help="Optional JSONL file with one BenchmarkRecord per trajectory.",
    )
    run.add_argument(
        "--agent-debug-jsonl",
        type=Path,
        help="JSONL file in the Trajectory contract consumed by AgentDebug-RH.",
    )
    run.add_argument(
        "--trajectories-dir",
        type=Path,
        help="Directory that receives one JSON file per executed trajectory.",
    )

    commands.add_parser("sync-prompts", help="Sync the local prompts to Langfuse.")

    validate_profile = commands.add_parser(
        "validate-profile",
        help="Validate a YAML profile and print its normalized form.",
    )
    validate_profile.add_argument("profile", type=Path, help="Profile YAML file.")

    dataset = commands.add_parser(
        "run-dataset",
        help="Generate a resumable campaign of Front B inputs.",
    )
    dataset.add_argument("--campaign", type=Path, required=True, help="Campaign YAML file.")
    dataset.add_argument("--model", help="OpenRouter model ID for a fixed-input campaign.")
    dataset.add_argument(
        "--limit",
        type=int,
        help="Limit collection to the first N planned trajectories.",
    )
    dataset.add_argument(
        "--max-parallel",
        type=int,
        help="Override the number of scenarios executed in parallel.",
    )
    dataset.add_argument(
        "--output-dir",
        type=Path,
        help="Override the campaign output directory.",
    )

    validate_campaign = commands.add_parser(
        "validate-dataset-campaign",
        help="Validate a dataset campaign and print its normalized form.",
    )
    validate_campaign.add_argument("campaign", type=Path, help="Campaign YAML file.")

    export = commands.add_parser(
        "export-trace",
        help="Export a complete Langfuse trace as a single JSON file.",
    )
    export.add_argument("--trace-id", required=True, help="Hexadecimal trace ID.")
    export.add_argument("--output", type=Path, help="JSON output file; prints to stdout when omitted.")

    return parser


def _pick(explicit: _T | None, profile_value: _T | None, default: _T) -> _T:
    return (
        explicit
        if explicit is not None
        else (profile_value if profile_value is not None else default)
    )


def _apply_profile(args: argparse.Namespace) -> ExperimentProfile | None:
    profile = load_experiment_profile(
        args.profile or Path(__file__).parent / "profiles" / "error_recovery.yaml"
    )
    pipeline = profile.pipeline if profile else None
    args.benign = _pick(args.benign, pipeline.benign_commands if pipeline else None, 3)
    args.malicious = _pick(args.malicious, pipeline.malicious_commands if pipeline else None, 0)
    resolved_pipeline = PipelineProfile(
        benign_commands=args.benign,
        malicious_commands=args.malicious,
    )
    validate_front_pipeline(profile.front if profile else None, resolved_pipeline)
    if profile:
        args.output = args.output or profile.artifacts.scenario_path
        args.jsonl = args.jsonl or profile.artifacts.benchmark_path
        args.agent_debug_jsonl = args.agent_debug_jsonl or profile.artifacts.agent_debug_path
        args.trajectories_dir = args.trajectories_dir or profile.artifacts.trajectory_path
    return profile


async def _run(args: argparse.Namespace) -> int:
    profile: ExperimentProfile | None = args.experiment_profile
    brief = args.brief or args.brief_file.read_text(encoding="utf-8")
    result = await ScenarioService().run(
        brief,
        benign_count=args.benign,
        malicious_count=args.malicious,
        research_front=profile.front if profile else None,
        experiment_profile=profile.name if profile else None,
    )
    if profile:
        print(f"Profile: {profile.name} (front={profile.front.value})")
    rendered = result.model_dump_json(indent=2, by_alias=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Scenario saved to {args.output}")
    else:
        print(rendered)
    if args.jsonl:
        args.jsonl.parent.mkdir(parents=True, exist_ok=True)
        lines = [record.model_dump_json(by_alias=True) for record in result.benchmark_records]
        args.jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Benchmark JSONL saved to {args.jsonl}")
    if args.agent_debug_jsonl:
        args.agent_debug_jsonl.parent.mkdir(parents=True, exist_ok=True)
        trajectories = scenario_trajectories(result)
        lines = [trajectory.model_dump_json(exclude_none=True) for trajectory in trajectories]
        args.agent_debug_jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"AgentDebug-RH trajectories saved to {args.agent_debug_jsonl}")
    if args.trajectories_dir:
        trajectories = scenario_trajectories(result)
        paths = save_trajectory_files(trajectories, args.trajectories_dir)
        print(f"{len(paths)} trajectories saved to {args.trajectories_dir}")
    passed = sum(item.benchmark_passed for item in result.executions)
    print(f"Result: {passed}/{len(result.executions)} trajectories passed the oracle.")
    flush_langfuse()
    return 0


def _export_trace(args: argparse.Namespace) -> int:
    from src.services.observability.export import fetch_trace_export  # noqa: PLC0415

    rendered = json.dumps(
        fetch_trace_export(args.trace_id),
        ensure_ascii=False,
        indent=2,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"Trace saved to {args.output}")
    else:
        print(rendered)
    return 0


def _validate_profile(args: argparse.Namespace) -> int:
    profile = load_experiment_profile(args.profile)
    print(profile.model_dump_json(indent=2))
    return 0


def _validate_dataset_campaign(args: argparse.Namespace) -> int:
    from src.services.dataset.profile import load_dataset_campaign_profile  # noqa: PLC0415

    campaign = load_dataset_campaign_profile(args.campaign)
    if campaign.fixed_inputs is not None:
        from src.services.dataset.fixed import load_fixed_inputs  # noqa: PLC0415

        load_fixed_inputs(campaign)
    print(campaign.model_dump_json(indent=2))
    return 0


async def _run_dataset(args: argparse.Namespace) -> int:
    from src.services.dataset.profile import load_dataset_campaign_profile  # noqa: PLC0415
    from src.services.dataset.service import ErrorRecoveryDatasetService  # noqa: PLC0415

    campaign = load_dataset_campaign_profile(args.campaign)
    if args.model:
        if campaign.generation is None:
            raise ValueError("--model requires a fixed-input campaign with a generation section.")
        from src.services.dataset.service import _safe_filename  # noqa: PLC0415

        campaign = campaign.model_copy(
            update={
                "generation": campaign.generation.model_copy(update={"model": args.model}),
                "output_dir": Path("outputs/front-a-300") / _safe_filename(args.model),
            }
        )
    summary = await ErrorRecoveryDatasetService().run(
        campaign,
        limit=args.limit,
        max_parallel=args.max_parallel,
        output_dir=args.output_dir,
    )
    destination = args.output_dir or campaign.output_dir
    print(f"Campaign: {campaign.name}")
    print(
        f"Result: {summary.recorded_executions}/{summary.planned_executions} "
        "Front B inputs generated."
    )
    if campaign.fixed_inputs is not None:
        print(f"Public detector dataset (no success field): {destination / 'detector-input.jsonl'}")
        print(f"Legacy contract (includes success): {destination / 'front-b-input.jsonl'}")
        print(f"Clean controls: {summary.successful_controls}")
    else:
        print(f"Public dataset: {destination / 'front-b-input.jsonl'}")
    print(f"Separate labels: {destination / 'labels.json'}")
    return 0 if summary.complete else 2


def main() -> int:  # noqa: PLR0911 - an explicit dispatcher keeps the commands isolated
    parser = _parser()
    args = parser.parse_args()
    if args.command == "sync-prompts":
        from src.prompts.sync import sync_prompts  # noqa: PLC0415

        return sync_prompts()
    if args.command == "validate-profile":
        try:
            return _validate_profile(args)
        except ValueError as exc:
            parser.error(str(exc))
    if args.command == "validate-dataset-campaign":
        try:
            return _validate_dataset_campaign(args)
        except ValueError as exc:
            parser.error(str(exc))
    if args.command == "run-dataset":
        try:
            return asyncio.run(_run_dataset(args))
        except ValueError as exc:
            parser.error(str(exc))
        finally:
            flush_langfuse()
    if args.command == "export-trace":
        return _export_trace(args)
    try:
        args.experiment_profile = _apply_profile(args)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        return asyncio.run(_run(args))
    finally:
        flush_langfuse()
