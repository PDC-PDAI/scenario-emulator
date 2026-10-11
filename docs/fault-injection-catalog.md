# Fault injection catalog

🇺🇸 **English** · [🇧🇷 Português](fault-injection-catalog.pt-BR.md) · [Home](../README.md)

This document describes how Scenario Emulator turns a successful baseline
trajectory into a controlled trajectory with a single critical cause. The goal
is to produce inputs that Front B can evaluate without exposing the label in the
public file.

## Implemented scope

The full taxonomy has 18 `(module, error type)` pairs across `memory`,
`reflection`, `planning`, `action` and `system`. Injection currently covers the
11 pairs that can be represented causally with the checkpoints produced today:

| Module | Implemented types |
| --- | --- |
| `planning` | `constraint_ignorance`, `impossible_action`, `inefficient_plan` |
| `action` | `misalignment`, `invalid_action`, `format_error`, `parameter_error` |
| `system` | `step_limit`, `tool_execution_error`, `llm_limit`, `environment_error` |

The seven `memory` and `reflection` pairs are not injected. The current
questionnaire agent only emits `planning` and `action`; adding post-processed
memory or reflection text would create modules that took no part in the decision
and would produce causally false ground truth.

`others/others` is not injected either. It remains an escape category for
natural failures that do not fit the taxonomy, not a synthetic class to balance.

## Common flow

1. The campaign runs the real agent until it gets a trajectory that passed the
   oracle and ended with `salvar_formulario`.
2. The runner assigns exactly one `fault_id` to that baseline.
3. The injector selects a variant of the fault and modifies only the steps
   needed to make the fault observable.
4. The resulting trajectory gets `success=false`.
5. The public input holds only `trajectory_id`, `task_description`,
   `environment`, `success` and `steps`.
6. The minimal label `{step, module, error_type}` is written separately to
   `labels.json`.
7. Operation, variant, source baseline and other details stay in
   `private/provenance.jsonl`.

Variants rotate by the occurrence number of each `fault_id` in the export. With
twenty examples per class and three variants, the split is `7/7/6`, independent
of the public ID.

Baselines are requested from the coordinator in batches of at most ten. The
target of 22 executions per scenario is filled by several batches (`10 + 10 + 2`)
so that a long model response cannot lose items and invalidate the whole attempt.

The injected plans, payloads and environment responses are in Portuguese, like
the agent's own trajectories; the variant IDs below name them.

## Planning faults

### `planning/constraint_ignorance`

**Critical point:** the baseline's terminal step.

The terminal plan is replaced by an explicit decision to disregard a constraint
available in the context. The action stays aligned with the plan and calls the
correct tool with valid JSON, but the payload is swapped for generic questions.
The response records that the questionnaire was persisted and that the oracle
rejected its semantic coverage.

Variants:

- `discard_job_requirements`: ignores the job's mandatory requirements;
- `discard_seniority`: ignores the seniority and the expected depth;
- `generic_questionnaire`: ignores the constraints of the coordinator's command.

The plan is the critical cause because the action faithfully executes the
defective strategy; there is no missing tool, invalid JSON or missing parameter.

### `planning/impossible_action`

**Critical point:** step 1.

The terminal save decision is moved before a required precondition. The tool
exists and the parameters have the expected format, but the environment refuses
the transition because the required state does not exist yet. The trajectory
ends at that step.

Variants:

- `save_before_lookup`: tries to save before looking up the job;
- `save_before_draft`: tries to save before producing the draft;
- `save_before_validation`: tries to save before validating the questionnaire.

This differs from `action/invalid_action`: the action exists. The defect is in
the strategy that chose it in a state where it cannot achieve the goal.

### `planning/inefficient_plan`

**Critical point:** step 2.

The first correct lookup is preserved. From step 2 on, the plan states that it
will look up the same data again, although it is already in the history, and the
action repeats `get_info_vaga`. No terminal step runs, so the task is not
completed.

Variants:

- `repeat_lookup_twice`: two repetitions, three steps in total;
- `repeat_lookup_three_times`: three repetitions, four steps in total;
- `repeat_lookup_five_times`: five repetitions, six steps in total.

The tool works and every action matches its plan. The error is the useless
repetition that blocks progress, not a failure of the action or the environment.

## Action faults

In the 220/1,100 campaigns, every `action` fault is applied to the baseline's
terminal step, and the original plan to persist the questionnaire is preserved.
The [fixed campaign](#fixed-300-sample-campaign) moves `invalid_action` and
`format_error` across the tool-call steps.

### `action/misalignment`

The valid action is replaced by another action that contradicts the plan of the
same step.

Variants:

- `register_failure`: records a failure when the plan says to save;
- `repeat_lookup`: repeats a lookup when the plan says to persist;
- `fetch_unknown_job`: looks up a different identifier instead of persisting.

The actions used exist and are serialized correctly. The error is the
disconnect between the stated intent and the executed operation.

### `action/invalid_action`

The `salvar_formulario` name is replaced by a tool that does not exist. The
original arguments are kept and the environment answers `TOOL_NOT_FOUND`.

Variants:

- `versioned_tool`: `salvar_formulario_v2`;
- `unknown_namespace`: `questionarios.salvar_formulario`;
- `invented_archive_tool`: `arquivar_formulario_definitivo`.

### `action/format_error`

The action's representation no longer satisfies the expected envelope, without
changing the plan.

Variants:

- `truncated_json`: an interrupted JSON object;
- `plain_text_call`: the call written as free text;
- `array_instead_of_object`: a JSON list in place of the action object.

`raw_output` remains an auditable JSON envelope. Inside it, the `action` value
preserves exactly the invalid representation the environment received.

### `action/parameter_error`

The correct tool is kept, but its arguments no longer satisfy the contract.

Variants:

- `missing_payload`: removes the mandatory `payload` argument;
- `wrong_questionnaire_id`: swaps the ID for another questionnaire's;
- `payload_wrong_type`: sends free text where an object should be.

## System faults

`system` errors are not agent decisions. Planning and action therefore stay
correct whenever a decision was produced. The main evidence is in the
environment's response or in the provider event.

### `system/step_limit`

**Critical point:** step 1.

The first correct step is preserved, but the runtime is configured with a
one-step cap and ends the execution before persistence.

Text variants:

- `max_steps_one`;
- `budget_exhausted`;
- `orchestrator_iteration_cap`.

All of them use the structured event `maximum_steps_reached`. There is no
preceding inefficient repetition, so the critical cause is the system limit.

### `system/tool_execution_error`

**Critical point:** the terminal step.

The `salvar_formulario` call and its arguments are preserved. The environment
records that the arguments were accepted, but the external dependency failed.

Variants:

- `external_503`: external service unavailable;
- `connection_reset`: connection dropped during the call;
- `empty_external_response`: the tool returns no result.

### `system/llm_limit`

**Critical point:** the expected terminal step.

The terminal decision is replaced by a provider event. That step holds only the
`system` module, because there was no complete planning/action to attribute to
the agent.

Variants:

- `provider_timeout`;
- `token_budget_exhausted`, with the partial output preserved;
- `context_window_exceeded`.

### `system/environment_error`

**Critical point:** the terminal step.

The agent emits a valid call and the environment accepts its arguments, but the
simulator's own state transition fails.

Variants:

- `state_commit_lost`: a change confirmed and then lost;
- `simulator_invariant_broken`: the later state violates the expected transition;
- `storage_partition`: internal storage unavailable during the commit.

The difference from `tool_execution_error` is the origin: there, the dependency
called by the tool fails; here, the defect is in the simulator or its state.

## Fixed 300-sample campaign

The [fixed campaign](../README.md#current-campaign-300-samples-per-model) uses the
same injectors with two differences.

**Step rotation.** `invalid_action`, `format_error`, `tool_execution_error` and
`llm_limit` rotate across the steps that hold a valid tool call. The first three
occurrences of a type use the first compatible step, one per variant; the next
three use the second compatible step, and so on, cycling. When the target is not the last
step, the trajectory is truncated after the injected step, so it never shows a
fictitious successful continuation. At an earlier step, `invalid_action` uses the
same three invented tool names and keeps the original arguments,
`format_error` renders the original tool name in the broken representation, and
`tool_execution_error` and `llm_limit` keep or replace that step's call as
described above. The other seven types keep their semantic position. With
two-step baselines this places 92 faults at step 1 and 148 at step 2; the actual
counts are in `private/distribution.json`, and the provenance records
`step_policy`, `compatible_steps` and `target_step`.

**Controls and blind input.** Sixty positions are clean controls with a `null`
label. `detector-input.jsonl` drops `success`, so the detector cannot tell
controls from faults; `front-b-input.jsonl` keeps the legacy contract with
`success`.

## Faults not implemented yet

A future modular rollout must emit `memory` and `reflection` explicitly from
step 2 on:

| Module | Pending types |
| --- | --- |
| `memory` | `over_simplification`, `memory_retrieval_failure`, `hallucination` |
| `reflection` | `progress_misjudge`, `outcome_misinterpretation`, `causal_misattribution`, `hallucination` |

Step 1 will keep only `planning` and `action`, as the Front B contract requires.
Once the new producer exists, mutations must contaminate the later decisions:
changing only the memory/reflection text without changing the executed path does
not establish a critical cause.

## Expanded campaign

The versioned `configs/campaigns/error-recovery-front-b-220.yaml` campaign
generates twenty instances of each of the eleven implemented pairs:

```bash
uv run scenario-emulator validate-dataset-campaign \
  configs/campaigns/error-recovery-front-b-220.yaml

uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-220.yaml \
  --max-parallel 10
```

For a low-cost smoke test, use a new directory and limit the collection:

```bash
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-220.yaml \
  --limit 11 \
  --output-dir outputs/error-recovery-front-b-220-smoke
```

Do not reuse that directory for the full campaign afterwards: the private
assignment is persisted and tied to the planned size.

## Offline expansion to 1,100 cases

The `configs/campaigns/error-recovery-front-b-1100.yaml` campaign reuses the 220
valid baselines of the smaller dataset without calling the agents or the
provider again:

```bash
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-1100.yaml
```

Two fields control this mode:

- `baseline_source_dir` points to a dataset that has the original batches in
  `private/baseline-scenarios`;
- `augmentations_per_baseline` sets how many injected trajectories each baseline
  produces.

In the 1,100-case configuration, each of the 220 baselines is used five times
and receives five distinct `fault_id` values. The stratified allocation produces:

- 100 cases per error type;
- variants balanced `34/33/33` per type;
- 110 cases per domain, 10 of each error type;
- 300 `planning` cases, 400 `action` cases and 400 `system` cases.

The reuse index lives only in `private/provenance.jsonl`, in the private fields
`injection.baseline_reuse_index` and `injection.baseline_reuse_total`.

When creating splits, always group by `parent_trajectory_id`. Every trajectory
derived from the same baseline must stay in the same split; otherwise the shared
text and steps leak between training and test.

Some baselines recovered from a natural error before succeeding, so their
injected fault sits at step 3; see [Labels at step 3](step-3-labels.md).

## Quality invariants

- a baseline must pass the oracle before injection;
- each trajectory receives only one planned critical cause;
- the `(module, error_type)` pair is validated against the taxonomy;
- steps stay contiguous and 1-indexed;
- the public ID does not contain the fault name;
- `fault_id`, operation and variant appear only in the private provenance;
- `labels.json` stays out of the directory delivered to the detector;
- expansions must keep the same `parent_trajectory_id` in the same split;
- variants of a class should be separated by template when forming training and
  test splits, so that near duplicates do not inflate the metric.
