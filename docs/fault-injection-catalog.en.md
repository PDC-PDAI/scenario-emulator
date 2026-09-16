# Fault injection catalog

[🇧🇷 Português](fault-injection-catalog.md) · 🇺🇸 **English** · [Home](../README.en.md)

The injector transforms a successful baseline into a controlled trajectory with
one planned critical cause. Public input and expected labels are exported separately.

## Implemented scope

The taxonomy has 18 module/type pairs across memory, reflection, planning, action,
and system. Eleven are injected today. The seven memory/reflection pairs are not
injected because the questionnaire agent only emits planning/action; adding
postprocessed text would fabricate causal evidence. `others/others` remains a
fallback for natural failures, not a balanced synthetic class.

## Common flow

1. Collect a real, oracle-approved execution ending with `salvar_formulario`.
2. Assign one `fault_id` to each derived trajectory.
3. Select a variant and mutate the necessary saved steps.
4. Set `success=false` on the derived trajectory.
5. Export only `trajectory_id`, `task_description`, `environment`, `success`, and `steps` publicly.
6. Export `{step, module, error_type}` separately to `labels.json`.
7. Store operation, variant, and baseline ancestry in `private/provenance.jsonl`.

Variants rotate by occurrence count per fault, independent of public IDs: twenty
cases with three variants yield 7/7/6. Coordinator requests are bounded at ten
baselines per batch; a scenario target of 22 uses 10 + 10 + 2 to limit incomplete
long model responses. These mutations do not execute new agent rollouts.

## Planning faults

| Class | Critical step | Mutation and causal distinction | Variants |
|---|---|---|---|
| `constraint_ignorance` | Baseline terminal | Plan explicitly discards an available requirement; valid save action follows that defective plan with generic questions. Environment records persistence and rejected semantic coverage. | `discard_job_requirements`, `discard_seniority`, `generic_questionnaire` |
| `impossible_action` | 1 | Move a valid save action before its required lookup, draft, or validation. Environment rejects the state transition. Tool exists; the plan selected an infeasible state. | `save_before_lookup`, `save_before_draft`, `save_before_validation` |
| `inefficient_plan` | 2 | Preserve first successful lookup, then repeatedly plan and execute the same lookup without a terminal action. Tools work; unnecessary repetition prevents completion. | `repeat_lookup_twice` (3 total steps), `repeat_lookup_three_times` (4), `repeat_lookup_five_times` (6) |

## Action faults

All apply to the baseline terminal step and retain the original plan to save.

| Class | Mutation and causal distinction | Variants |
|---|---|---|
| `misalignment` | Replace save with a valid but contradictory action. Serialization and available tools remain valid. | `register_failure`, `repeat_lookup`, `fetch_unknown_job` |
| `invalid_action` | Replace tool name while preserving arguments; environment returns `TOOL_NOT_FOUND`. | `versioned_tool` → `salvar_formulario_v2`; `unknown_namespace` → `questionarios.salvar_formulario`; `invented_archive_tool` → `arquivar_formulario_definitivo` |
| `format_error` | Action no longer satisfies the expected envelope. `raw_output` remains complete JSON containing the invalid action representation. | `truncated_json`, `plain_text_call`, `array_instead_of_object` |
| `parameter_error` | Correct tool receives invalid arguments. | `missing_payload`, `wrong_questionnaire_id`, `payload_wrong_type` |

## System faults

System faults are not agent decisions. Planning/action stay correct whenever a
complete decision exists; environmental/provider evidence identifies the failure.

| Class | Critical step | Mutation | Variants |
|---|---|---|---|
| `step_limit` | 1 | Preserve successful first step, terminate before persistence with `maximum_steps_reached`; no prior inefficient loop. | `max_steps_one`, `budget_exhausted`, `orchestrator_iteration_cap` |
| `tool_execution_error` | Terminal | Preserve valid save call; arguments accepted but an external dependency fails. | `external_503`, `connection_reset`, `empty_external_response` |
| `llm_limit` | Expected terminal | Replace complete decision with provider event; only `system` exists in this step. Partial output is preserved when applicable. | `provider_timeout`, `token_budget_exhausted`, `context_window_exceeded` |
| `environment_error` | Terminal | Valid call accepted, but simulator/internal state transition fails, unlike an external tool dependency. | `state_commit_lost`, `simulator_invariant_broken`, `storage_partition` |

## Not implemented

| Module | Pending classes |
|---|---|
| `memory` | `over_simplification`, `memory_retrieval_failure`, `hallucination` |
| `reflection` | `progress_misjudge`, `outcome_misinterpretation`, `causal_misattribution`, `hallucination` |

A future modular rollout must emit these modules explicitly from step 2 onward
and propagate their defects into later decisions. Merely changing their text
does not establish a critical cause. Step 1 continues to contain planning/action.

## Campaigns and offline expansion

```bash
uv run scenario-emulator validate-dataset-campaign \
  configs/campaigns/error-recovery-front-b-220.yaml
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-220.yaml --max-parallel 10
```

For a small collection, pass `--limit 11 --output-dir outputs/front-b-smoke`.
Do not reuse that directory for a full run: private assignments depend on the
planned size.

```bash
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-1100.yaml
```

`baseline_source_dir` selects the dataset containing original batches under
`private/baseline-scenarios`. `augmentations_per_baseline: 5` makes five derived
trajectories with distinct fault IDs per baseline, without model/provider calls.
The 220 baselines produce 1,100 trajectories: 100 per class; variant balance
34/33/33; 110 per scenario with 10 per class; 300 planning, 400 action, 400 system.
Private `injection.baseline_reuse_index` and `baseline_reuse_total` record reuse.

## Quality invariants

- Baselines pass the oracle before injection; each derivative has one planned critical cause.
- Module/type pairs satisfy the taxonomy; indices stay contiguous and 1-indexed.
- Public IDs omit fault names; fault IDs, operations, and variants remain private.
- Labels remain outside detector input.
- Group all derivatives of one `parent_trajectory_id` into the same split.
- Also separate variant templates across training/test splits to avoid inflated metrics from near duplicates.
