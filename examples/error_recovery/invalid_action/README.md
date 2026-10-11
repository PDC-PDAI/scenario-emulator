# Error Recovery Case ER-01 — `action/invalid_action`

🇺🇸 **English** · [🇧🇷 Português](README.pt-BR.md) · [Home](../../../README.md)

## Purpose

This case checks whether the AgentDebug-RH pipeline localizes a tool-selection
failure where it arises, without blaming the correct planning or the earlier
steps.

The artifact is self-contained and synthetic, and contains no credentials, PII
or dependency on files in `outputs/`. It can run right after cloning both
repositories.

## Experimental hypothesis

If the planning selects the correct operation but the action calls a tool that
does not exist, the diagnosis must point to:

| Field | Expected value |
| --- | --- |
| Critical step | `2` |
| Module | `action` |
| Type | `invalid_action` |
| Task outcome | `false` |

## How the error is built

The trajectory has two steps:

1. `get_info_vaga` looks up the job successfully;
2. the planning correctly decides to use `salvar_formulario`, but the action is
   changed to `salvar_formulario_v2`. The environment answers `TOOL_NOT_FOUND`.

The only controlled variable is the tool name in the `action` module of step 2.
The earlier context, the planning and the step index stay valid. This reduces the
ambiguity between `planning/constraint_ignorance` and `action/invalid_action`.

Here, a step means the full cycle `context → planning → action → environment
response`. Each tool call therefore takes one step, not three. `raw_output`
preserves planning and action in the same envelope, before they are split into
`module_outputs`.

## Prerequisites

Lay out the checkouts as sibling directories:

```text
workspace/
├── scenario-emulator/
└── agentdebug-rh/
```

In `agentdebug-rh`, install the dependencies and configure the provider as
described in that project's own `.env.example`:

```bash
uv sync
cp .env.example .env
```

Review the JSONL before running: the pipeline sends its content to the provider
configured in AgentDebug-RH.

## Run

From the `agentdebug-rh` directory:

```bash
uv run python src/main.py \
  ../scenario-emulator/examples/error_recovery/invalid_action/agent-debug.jsonl \
  --output-dir output/cases/error-recovery-invalid-action \
  --max-parallel 1 \
  --max-attempts 1 \
  --print-diagnosis
```

`--max-attempts 1` runs module detection, root-cause selection and feedback
generation. It does not attempt a re-rollout.

## Acceptance criteria

The case passes when:

1. the file is accepted by the `Trajectory` schema without conversion;
2. the root cause is attributed to step 2;
3. the critical module is `action`;
4. the type is `invalid_action`;
5. the feedback recommends using `salvar_formulario`, without changing step 1.

Compare the produced diagnosis with
[`expected-diagnosis.json`](expected-diagnosis.json). Because the classification
is made by an LLM, record disagreements as experimental results; do not silently
fix them in the fixture. The file is a partial oracle, not a `TrajectoryDiagnosis`
dump; the field mapping is documented in
[`docs/data-contracts/agentdebug.md`](../../../docs/data-contracts/agentdebug.md).

## Artifacts

| File | Use |
| --- | --- |
| [`agent-debug.jsonl`](agent-debug.jsonl) | Direct input for the AgentDebug-RH CLI |
| [`trajectory.json`](trajectory.json) | The same trajectory as readable JSON |
| [`manifest.json`](manifest.json) | Injection operation and ground truth |
| [`expected-diagnosis.json`](expected-diagnosis.json) | Comparison oracle |

## Limitation

This case tests detection, root cause and remediation. It does not demonstrate
full recovery: `replay.performed=false` while the HTTP contract of the
re-rollout adapter is not implemented.

An equivalent execution was published to Langfuse to inspect the hierarchy:
[error-recovery-injected-trajectory](https://fuse-agents.sissa.ufg.br/project/cmshwjvwp0019of07n2hc6rs0/traces/5cd1b7be742a2348b156f033e32d9b67).
Access to that deployment is not needed to run this case.
