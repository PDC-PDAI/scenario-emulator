# Error Recovery

🇺🇸 **English** · [🇧🇷 Português](error-recovery.pt-BR.md) · [Home](../README.md)

The Error Recovery work produces reproducible trajectories for locating and
diagnosing agent failures with AgentDebug-RH. Scenario Emulator acts as
**Front A**, which generates and annotates the data; AgentDebug-RH acts as
**Front B**, which detects the critical cause and proposes a remediation.

The work uses the profile
[`configs/fronts/error_recovery.yaml`](../configs/fronts/error_recovery.yaml).

## Current scope

The profile captures per-step ReAct checkpoints and exports the `Trajectory`
contract. The implemented catalog holds eleven faults across `planning`,
`action` and `system`:

| Module | Error types |
| --- | --- |
| `planning` | `constraint_ignorance`, `impossible_action`, `inefficient_plan` |
| `action` | `misalignment`, `invalid_action`, `format_error`, `parameter_error` |
| `system` | `step_limit`, `tool_execution_error`, `llm_limit`, `environment_error` |

The `run` command generates real baselines. Balanced injection happens in the
runner described in [Front B campaigns](#front-b-campaigns). The
[fault injection catalog](fault-injection-catalog.md) documents the mechanism,
the variants and the causal limits of each class.

Re-rollout is not executed in this repository. The `usecases-service` offers
`POST /executions/{id}/re-rollout`, reuses the prefix saved in `messages` and
creates a child trajectory with `lineage`. That is why `replay_enabled` stays
`false` in the profile: enabling it here would suggest a capability that still
belongs to Front C.

## Generate trajectories

```bash
uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL"
```

The default profile generates three benign commands, creates no candidate
answers and keeps the evaluator disabled. The artifacts are written to:

```text
outputs/error_recovery/
├── scenario.json
├── benchmark.jsonl
├── agent-debug.jsonl
└── trajectories/
```

To keep a run separate, override every path:

```bash
uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL" \
  --benign 1 \
  --malicious 0 \
  --output outputs/agentdebug-e2e/scenario.json \
  --jsonl outputs/agentdebug-e2e/benchmark.jsonl \
  --agent-debug-jsonl outputs/agentdebug-e2e/agent-debug.jsonl \
  --trajectories-dir outputs/agentdebug-e2e/trajectories
```

The brief is in Portuguese because the agent, its prompts and its tools work in
Brazilian Portuguese.

## Contract with AgentDebug-RH

The normative definition of `step`, the input and output schemas and the metrics
matrix are in [`docs/data-contracts/agentdebug.md`](data-contracts/agentdebug.md).

Each execution persists two levels of information:

- `failure_annotation`: the emulator's deterministic ground truth, with code,
  step, module, error type, evidence and a retry flag;
- `agent_debug_trajectory`: the self-contained input of the diagnosis pipeline,
  in the `Trajectory` format used by AgentDebug-RH.

Each tool call becomes one 1-indexed `TrajectoryStep`. The step gathers the
context, the observable module outputs (`planning` and `action`) and the tool
response in `env_response`. Memory and reflection are not invented when the agent
does not emit them. If there is no terminal tool, an explicit last step preserves
the failure.

Questionnaire trajectories also carry `messages`: the ordered conversation with
the real roles, content and `tool_call_id` values. The contract is normalized,
not byte-for-byte: JSON arguments become objects and internal provider metadata
is removed. This history is the prefix Front C uses in a re-rollout.

The `success` field is the scenario oracle's outcome. A correct refusal is a
success; over-refusal and undue compliance are failures. The annotation is the
experimental ground truth, not a root-cause diagnosis: that responsibility stays
with AgentDebug-RH.

## Front A → B smoke test

A self-contained `action/invalid_action` case lives in
[`examples/error_recovery/invalid_action/`](../examples/error_recovery/invalid_action/README.md).
It works from a clean clone and does not depend on `outputs/`.

Keep both checkouts as siblings:

```text
PDC-PDAI/
├── scenario-emulator/
└── agentdebug-rh/
```

After installing Front B and configuring its provider, run:

```bash
cd ../agentdebug-rh
uv run python src/main.py \
  ../scenario-emulator/examples/error_recovery/invalid_action/agent-debug.jsonl \
  --output-dir ../scenario-emulator/outputs/agentdebug-smoke \
  --max-parallel 1 \
  --max-attempts 1 \
  --print-diagnosis
```

The expected result points to `step 2 / action / invalid_action`, with a root
cause and a remediation. Compare the output with
[`expected-diagnosis.json`](../examples/error_recovery/invalid_action/expected-diagnosis.json).

To analyze a freshly generated trajectory:

```bash
cd ../agentdebug-rh
uv run python src/main.py \
  ../scenario-emulator/outputs/agentdebug-e2e/agent-debug.jsonl \
  --output-dir ../scenario-emulator/outputs/agentdebug-e2e/diagnoses \
  --max-parallel 1 \
  --max-attempts 1 \
  --print-diagnosis
```

Front B skips trajectories whose `success` is `true`, since there is no failure
to diagnose. Use the controlled case to validate root cause and remediation; use
the fresh flow to validate the export and the contract loading. Measuring false
positives requires the explicit blind mode in the
[v2 protocol](dataset-v2-success-controls.md#blind-evaluation).

## Front B campaigns

The historical
[`error-recovery-front-b-100.yaml`](../configs/campaigns/error-recovery-front-b-100.yaml)
campaign generates 100 cases of the four `action` classes. The main
[`error-recovery-front-b-220.yaml`](../configs/campaigns/error-recovery-front-b-220.yaml)
campaign runs ten themes, collects 220 valid baselines and assigns twenty cases
to each of the eleven implemented classes. The runner replaces natural baselines
that fail and reuses saved batches when it runs again.

```bash
uv run scenario-emulator validate-dataset-campaign \
  configs/campaigns/error-recovery-front-b-220.yaml

uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-220.yaml \
  --max-parallel 10
```

The
[`error-recovery-front-b-1100.yaml`](../configs/campaigns/error-recovery-front-b-1100.yaml)
campaign expands the 220 baselines offline. Each baseline receives five distinct
faults, producing 1,100 trajectories and one hundred cases per class with no new
model calls:

```bash
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-1100.yaml
```

Output:

```text
outputs/error-recovery-front-b-1100/
├── front-b-input.jsonl
├── front-b-inputs/
├── labels.json
└── private/
```

`front-b-input.jsonl` and `front-b-inputs/` are the public inputs, with neutral
IDs and no expected label. Send `labels.json` separately, for evaluation only;
never put it in the directory the detector reads. `private/` keeps baselines,
provenance, the manifest and checkpoints. Historical outputs are not versioned.
The [v2 extension](dataset-v2-success-controls.md) adds reviewed success controls,
and the [fixed 300-sample campaign](../README.md#current-campaign-300-samples-per-model)
adds blind detector input with clean controls for each model.

## Observability and troubleshooting

With Langfuse configured, filter traces by the `error_recovery` or `trajectory`
tags, or by the profile name. The outputs include the full trajectory and the
failure annotation, while the JSON files in `trajectories/` remain the canonical
source for interchange and replay.

Detailed Agno debugging is optional:

```bash
AGNO_DEBUG=true AGNO_DEBUG_LEVEL=2 \
  uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL"
```

Use it only locally, because it can print prompts and tool arguments. The
AgentDebug-RH contract is captured even with `AGNO_DEBUG=false`.

If Front B answers `Unsupported parameter: 'temperature'`, pick a compatible
model or a Front B version that omits that parameter. Beyond the exit code,
check that the report shows zero failed calls and that
`module_analyses[*].status` is `ok`.

## Architecture and development

### Responsibilities

The CLI in `src/cli.py` loads strict YAML profiles and applies overrides before
calling the services. Profile and campaign paths are relative to the working
directory: run the examples from the repository root.

| Layer | Main files | Contract |
| --- | --- | --- |
| Configuration | `src/settings.py`, `src/services/experiment/profile.py` | `.env`, provider, profiles and capabilities |
| Preparation | `src/services/job_description/service.py`, `src/services/coordinator_prompt/service.py` | Structured job and commands |
| Observed agent | `src/services/questionnaire/service.py` | Job lookup and terminal questionnaire/failure tool |
| Capture | `src/services/observability/react.py`, `src/agents/utils.py` | Events, tool calls and normalized messages |
| Export | `src/services/agent_debug/service.py` | `AgentDebugTrajectory` and deterministic annotation |
| Campaigns | `src/services/dataset/service.py`, `src/services/dataset/fixed.py` | Baselines, injections, resume, labels and provenance |
| Contracts | `src/schemas/agent_debug/schema.py`, `src/schemas/dataset/schema.py` | Taxonomy, steps and valid configuration |

`ScenarioService.run` chains the job creation, the benign commands and the
questionnaire agent execution to generate Front A baselines.

### Real execution and dataset construction

Real execution uses Agno and local tools, producing checkpoints and the
normalized conversation in `messages`. Each decision cycle with a tool call and
its response takes one step; infrastructure events do not become separate steps.

`ErrorRecoveryDatasetService` collects baselines approved by the oracle and
finished with `salvar_formulario`. Collection uses bounded batches, persists
progress and replaces baselines that fail. Then `inject_fault` transforms copies
of the checkpoints: it does not trigger a new interaction between the model and
the altered environment.

Offline expansion reuses the batches in `private/baseline-scenarios` from the
earlier dataset. Without those files, the 1,100-case YAML cannot rebuild the
historical campaign from the public JSONL files alone. Re-rollout is not executed
here.

### Change a fault

1. Check the module/type pair in the schema taxonomy and in the Front B contract.
2. Declare the mode in the `configs/fronts/error_recovery.yaml` profile.
3. Implement the mutation in `src/services/dataset/service.py`, including variants
   and private provenance. Preserve the steps before the critical cause when
   applicable.
4. Update the campaign that will use the class and the catalog in both languages.
5. Test the observable effect, the critical step, the causal alignment and the
   label separation.

Do not create post-processed `memory` or `reflection`: the current agent does not
emit those modules. A future producer has to emit them and show their effect on
the decisions.

### Change contracts or prompts

Keep indices contiguous from 1, complete `raw_output` envelopes and normalized
`messages`. A schema change requires checking the AgentDebug-RH consumer and
updating the self-contained example. `failure_annotation` is experimental ground
truth; root-cause diagnosis is Front B output.

Local prompts live in `src/prompts/raw_prompts.py`; Langfuse can replace them by
name and label. Record the versions actually used when comparing campaigns.
Prompt or provider changes alter the baseline distribution, even with the same
YAML. No seed guarantees an exact repetition of a remote LLM's content.

Prompts, tool docstrings and responses, injected texts and every string written
into trajectories, annotations, audits or manifests stay in Portuguese, the
language of the agent. Comments, docstrings, CLI help, logs and exceptions are
in English.

### Verify

```bash
uv run ruff check .
uv run pytest -q
uv run scenario-emulator validate-profile configs/fronts/error_recovery.yaml
uv run scenario-emulator validate-dataset-campaign configs/campaigns/error-recovery-front-b-220.yaml
```

`test_dataset_campaign.py` covers injection, variants, export and resume;
`test_fixed_dataset.py` and `test_provider_resume.py` cover the fixed campaign
and its routed resume; `test_budget_transport.py` and `test_exact_count_tools.py`
cover the Qwen budget and exact-count tools; `test_agent_debug_contract.py` and
`test_agent_messages.py` cover the contracts; `test_error_recovery_examples.py`
validates the fixture; the v2 control tests cover the audit and the selection
integrity. Models are replaced by fakes in the tests.

The v2 control scripts audit and select successful trajectories to measure false
positives. See the [v2 protocol](dataset-v2-success-controls.md) for sources,
selection criteria and limitations.

### Local diagnosis

`AGNO_DEBUG=true AGNO_DEBUG_LEVEL=2` enables detailed logs; checkpoints do not
need it. Without Langfuse, the local JSON files remain the interchange artifacts.
If a campaign ends with exit code 2, check the summary and the saved attempts:
the planned target is not complete yet. Use a new directory for `--limit` smoke
runs; do not reuse their private assignment for the full campaign.
