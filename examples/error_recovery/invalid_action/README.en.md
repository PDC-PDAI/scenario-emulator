# Error Recovery Case ER-01 — `action/invalid_action`

[🇧🇷 Português](README.md) · 🇺🇸 **English**

## Purpose and hypothesis

This self-contained synthetic fixture tests whether AgentDebug-RH localizes a
tool-selection failure without blaming correct planning or earlier steps. It
requires no `outputs/` files and contains no real credentials or personal data.

| Field | Expected value |
|---|---|
| Critical step | `2` |
| Module | `action` |
| Type | `invalid_action` |
| Task success | `false` |

Step 1 successfully calls `get_info_vaga`. Step 2 correctly plans to call
`salvar_formulario`, but action calls `salvar_formulario_v2`, and the environment
returns `TOOL_NOT_FOUND`. Only the action's tool name is controlled; context,
planning, and index remain valid. A step includes context, planning, action, and
environment response; `raw_output` preserves planning/action in one envelope.

## Run

Keep `scenario-emulator/` and `agentdebug-rh/` as sibling checkouts. In AgentDebug-RH,
run `uv sync`, copy its `.env.example` to `.env`, and configure the provider.
The diagnostic pipeline sends fixture content to that provider.

```bash
uv run python src/main.py \
  ../scenario-emulator/examples/error_recovery/invalid_action/agent-debug.jsonl \
  --output-dir output/cases/error-recovery-invalid-action \
  --max-parallel 1 --max-attempts 1 --print-diagnosis
```

One attempt performs module detection, critical-cause selection, and feedback;
it does not re-rollout.

## Acceptance

The consumer accepts the JSONL without conversion and identifies step 2, action,
invalid_action. Feedback recommends `salvar_formulario` without changing step 1.
Compare with `expected-diagnosis.json` using the mapping in the
[data contract](../../../docs/data-contracts/agentdebug.en.md). Record model
classification disagreements as experimental outcomes; do not silently edit the fixture.

| File | Purpose |
|---|---|
| [agent-debug.jsonl](agent-debug.jsonl) | Direct CLI input |
| [trajectory.json](trajectory.json) | Readable equivalent trajectory |
| [manifest.json](manifest.json) | Injection and ground truth |
| [expected-diagnosis.json](expected-diagnosis.json) | Partial comparison oracle |

## Limitation

This demonstrates a diagnostic test case, not complete recovery:
`replay.performed=false`. The Portuguese companion links a historical Langfuse
execution for inspection; access to that deployment is not required to run this fixture.
