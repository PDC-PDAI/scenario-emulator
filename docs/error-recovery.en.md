# Error Recovery

[🇧🇷 Português](error-recovery.md) · 🇺🇸 **English** · [Home](../README.en.md)

Scenario Emulator is **Front A**: it generates and annotates trajectories for
failure localization. **Front B**, AgentDebug-RH, detects the critical cause and
proposes remediation. The [error recovery profile](../configs/fronts/error_recovery.yaml)
captures ReAct checkpoints and exports the `Trajectory` contract.

## Scope

Eleven implemented fault classes cover `planning`, `action`, and `system`; see
the [fault catalog](fault-injection-catalog.en.md). `run` produces real baselines;
`run-dataset` constructs balanced synthetic failures from saved checkpoints.

Re-rollout is not implemented here. The integration described by the project uses
`POST /executions/{id}/re-rollout` in `usecases-service`, reuses a saved `messages`
prefix, and creates a child trajectory with lineage. `replay_enabled` remains
false because replay belongs to Front C.

## Generate trajectories

```bash
uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL"
```

Defaults: three benign commands, no candidate answers, evaluator disabled.
Outputs under `outputs/error_recovery/` are `scenario.json`, `benchmark.jsonl`,
`agent-debug.jsonl`, and `trajectories/`.

Preserve a separate run by overriding all artifact paths:

```bash
uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL" \
  --benign 1 --malicious 0 \
  --output outputs/agentdebug-e2e/scenario.json \
  --jsonl outputs/agentdebug-e2e/benchmark.jsonl \
  --agent-debug-jsonl outputs/agentdebug-e2e/agent-debug.jsonl \
  --trajectories-dir outputs/agentdebug-e2e/trajectories
```

## Contract with AgentDebug-RH

The [data contract](data-contracts/agentdebug.en.md) defines steps, input/output
schemas, and metrics. Every execution has deterministic `failure_annotation`
and a self-contained `agent_debug_trajectory` for diagnosis.

Each tool call becomes one 1-indexed step, including context, observable planning
and action, and tool response in `env_response`. Unobserved memory/reflection are
not invented. If there is no terminal tool, an explicit final step preserves the
failure. `messages` stores ordered roles, content, and actual `tool_call_id` values.
This is a normalized contract, not a byte-for-byte provider transcript: JSON tool
arguments become objects and provider internals are removed.

`success` is the scenario oracle outcome. Correct refusal is success; over-refusal
or unsafe compliance is failure. Annotation is experimental ground truth, not
Front B's predicted root cause.

## A → B smoke test

Use the [self-contained invalid-action example](../examples/error_recovery/invalid_action/README.en.md)
from a clean clone. Keep `scenario-emulator/` and `agentdebug-rh/` as sibling
checkouts, install Front B dependencies, and configure its provider:

```bash
cd ../agentdebug-rh
uv run python src/main.py \
  ../scenario-emulator/examples/error_recovery/invalid_action/agent-debug.jsonl \
  --output-dir ../scenario-emulator/outputs/agentdebug-smoke \
  --max-parallel 1 --max-attempts 1 --print-diagnosis
```

Expected localization: `step 2 / action / invalid_action`, with cause and remediation.
Compare against the example's `expected-diagnosis.json`. To inspect a fresh run,
replace input with `outputs/agentdebug-e2e/agent-debug.jsonl` and select a fresh
diagnosis output directory.

The documented Front B flow skips trajectories with `success=true`. Use the
controlled example to verify diagnosis and a fresh baseline to verify export and
contract loading. Evaluating false positives requires the explicit blind mode
in the [v2 protocol](dataset-v2-success-controls.en.md).

## Campaigns

The historical [100-case campaign](../configs/campaigns/error-recovery-front-b-100.yaml)
covers four action classes. The main [220-case campaign](../configs/campaigns/error-recovery-front-b-220.yaml)
collects 220 valid baselines across ten scenarios, with twenty cases per fault
class. Failed natural baselines are replaced; saved batches are reused on resume.

```bash
uv run scenario-emulator validate-dataset-campaign \
  configs/campaigns/error-recovery-front-b-220.yaml
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-220.yaml --max-parallel 10
```

The [1,100-case campaign](../configs/campaigns/error-recovery-front-b-1100.yaml)
reuses those 220 baselines offline, applying five distinct faults to each:

```bash
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-1100.yaml
```

```text
outputs/error-recovery-front-b-1100/
├── front-b-input.jsonl
├── front-b-inputs/
├── labels.json
└── private/
```

Public inputs contain neutral IDs without expected labels. Deliver `labels.json`
separately for evaluation, outside detector input. `private/` retains baselines,
provenance, manifest, and checkpoints. Historical outputs are not versioned.
The [v2 extension](dataset-v2-success-controls.en.md) adds reviewed success controls.

## Observability and troubleshooting

With Langfuse, filter by `error_recovery`, `trajectory`, or profile name. Local
trajectory JSONs remain canonical for interchange. Detailed Agno logging is optional:

```bash
AGNO_DEBUG=true AGNO_DEBUG_LEVEL=2 \
  uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL"
```

Debug logs can print prompts/tool arguments; capture works with debug disabled.
If Front B returns `Unsupported parameter: 'temperature'`, select a compatible
model or a consumer version that omits it. Check failed-call counts and
`module_analyses[*].status == "ok"`, not just the process exit code.

## Architecture and development

## Responsibilities

`src/cli.py` loads strict YAML profiles and applies overrides before calling
services. Profile/campaign paths are relative to the working directory: run
examples from the repository root.

| Layer | Main files | Contract |
|---|---|---|
| Configuration | `src/settings.py`, `src/services/experiment/profile.py` | `.env`, provider, profiles, and capabilities |
| Preparation | `src/services/job_description/service.py`, `src/services/coordinator_prompt/service.py` | Structured job and commands |
| Observed agent | `src/services/questionnaire/service.py` | Job lookup and terminal questionnaire/failure tool |
| Capture | `src/services/observability/react.py`, `src/agents/utils.py` | Events, tool calls, and normalized messages |
| Export | `src/services/agent_debug/service.py` | `AgentDebugTrajectory` and deterministic annotation |
| Campaigns | `src/services/dataset/service.py` | Baselines, injections, resume, labels, and provenance |
| Contracts | `src/schemas/agent_debug/schema.py`, `src/schemas/dataset/schema.py` | Taxonomy, steps, and valid configuration |

`ScenarioService.run` orchestrates baseline generation for `error_recovery`.
Security generation/evaluation services and their API live only in RecruitSecBench.

### Real execution and dataset construction

Real execution uses Agno and local tools, producing checkpoints and normalized
conversation in `messages`. Each decision cycle with its tool call and response
is one step; infrastructure events do not become independent steps.

`ErrorRecoveryDatasetService` collects oracle-approved baselines ending in
`salvar_formulario`, using bounded batches, persisted progress, and replacement
of failed baselines. `inject_fault` then transforms checkpoint copies; it does
not cause a fresh model interaction with the altered environment.

Offline expansion reuses `private/baseline-scenarios` batches from the earlier
dataset. Without them, the 1,100-case YAML cannot reconstruct the historical
campaign from public JSONLs alone. Re-rollout is not executed here.

### Change a fault

1. Check the module/type pair in the schema taxonomy and Front B contract.
2. Declare the mode in `configs/fronts/error_recovery.yaml`.
3. Implement the mutation in `src/services/dataset/service.py`, including variants
   and private provenance. Preserve preceding steps where appropriate.
4. Update the relevant campaign and both language versions of the catalog.
5. Test observable effects, critical step, causal alignment, and label separation.

Do not synthesize postprocessed `memory` or `reflection`: the current agent does
not emit these modules. A future producer must emit them and show downstream effects.

### Change contracts or prompts

Preserve contiguous indices starting at 1, complete `raw_output` envelopes, and
normalized `messages`. Schema changes require checking the AgentDebug-RH consumer
and updating the self-contained example. `failure_annotation` is experimental
ground truth; root-cause diagnosis is Front B output.

Local prompts live in `src/prompts/raw_prompts.py`; Langfuse can replace them by
name/label. Record actual prompt versions when comparing campaigns. Prompt/provider
changes alter baseline distributions even with the same YAML. No seed guarantees
identical remote LLM content.

### Verify

```bash
uv run ruff check .
uv run pytest -q
uv run scenario-emulator validate-profile configs/fronts/error_recovery.yaml
uv run scenario-emulator validate-dataset-campaign configs/campaigns/error-recovery-front-b-220.yaml
```

`test_dataset_campaign.py` covers injection, variants, export, and resume;
`test_agent_debug_contract.py` and `test_agent_messages.py` cover contracts;
`test_error_recovery_examples.py` validates the fixture; v2 control tests cover
audit and selection integrity. Tests substitute models.

The v2 control scripts/documentation were pre-existing local work when this
separation was prepared. They are not part of the security port. See the
[v2 protocol](dataset-v2-success-controls.en.md) for sources and limitations.

### Local diagnosis

`AGNO_DEBUG=true AGNO_DEBUG_LEVEL=2` enables detailed logs; checkpoints do not
require this setting. Local JSONs remain interchange artifacts without Langfuse.
Campaign exit code 2 means the planned target is incomplete; inspect the summary
and persisted attempts. Use a fresh directory for `--limit` smoke runs; do not
reuse their private assignments for a full campaign.
