# Scenario Emulator → AgentDebug-RH data contract

🇺🇸 **English** · [🇧🇷 Português](agentdebug.pt-BR.md) · [Home](../../README.md)

This document records the boundary between Front A (`scenario-emulator`) and
Front B (`agentdebug-rh`). Front A's executable schema is in
`src/schemas/agent_debug/schema.py`; the consumer keeps the equivalent schema in
`src/schemas/observability/execution.py`.

## Execution unit: `step`

A `step` is **one logical decision cycle of the agent**, not an atomic
infrastructure operation:

```text
step context
  → memory/reflection, when emitted
  → planning
  → action
  → env_response
```

Planning, the tool call and the tool response therefore belong to the same step.
The next step starts only when the agent decides again with the previous
environment response available. In single-inference agents, such as the
structured generators and the evaluator, the whole inference is a single step.

Indices are contiguous and 1-indexed. Step 1 must not invent `memory` or
`reflection`, because there is no execution history yet.

## Front B input: `AgentDebugTrajectory`

```json
{
  "trajectory_id": "exec-001",
  "task_description": "Selecionar candidato",
  "environment": "sistema de RH",
  "success": false,
  "steps": [
    {
      "index": 1,
      "module_outputs": {
        "planning": "Plano produzido no primeiro step",
        "action": "{\"tool\":\"buscar_candidatos\",\"arguments\":{}}"
      },
      "step_input": "Contexto disponível antes da decisão",
      "env_response": "Resposta recebida depois da ação",
      "raw_output": "{\"planning\":\"Plano produzido no primeiro step\",\"action\":{\"tool\":\"buscar_candidatos\",\"arguments\":{}}}"
    }
  ]
}
```

The values are in Portuguese, like the real trajectories.

Trajectory fields:

| Field | Meaning |
| --- | --- |
| `trajectory_id` | Stable identifier that correlates execution, diagnosis and trace. |
| `task_description` | Goal against which the agent's behavior is judged. |
| `environment` | Agent/environment in which the trajectory was produced. |
| `success` | Task outcome according to the scenario oracle, not to the diagnosis. |
| `steps` | Decision cycles in the order they happened. |

Step fields:

| Field | Meaning |
| --- | --- |
| `index` | Contiguous, 1-indexed position. |
| `module_outputs` | Observable outputs split by module. Missing modules are not fabricated. |
| `step_input` | Context available before the step's decision. |
| `env_response` | Environment result after the same step's action. |
| `raw_output` | Complete observable output from which the modules were extracted. |

When the SDK provides the model's whole output, `raw_output` preserves it. When
streaming delivers planning and the tool call as separate events, Front A stores
a JSON envelope with every observable fragment. It must not repeat only `action`
if `planning` was also extracted. The field may be empty only when the execution
fails before producing any agent output. The envelope is always serialized whole:
producers never truncate it midway, which keeps the JSON valid even when the
action carries large payloads.

Questionnaire trajectories may also carry normalized `messages`, the prefix
handed to Front C. Public campaign exports deliberately keep only the five
top-level fields above; the private originals keep the richer context.

## How analyzable modules are identified

Front A does not send a parallel list. Front B identifies the modules by the
non-empty keys of `module_outputs`, using exactly the taxonomy names:

- `memory`
- `reflection`
- `planning`
- `action`
- `system`
- `others`

In the current fine-analysis stage, `ANALYZED_MODULES` holds `memory`,
`reflection`, `planning` and `action`. `system` is assigned from environment
evidence at the root-cause stage; `others` is the escape category. Front B
defines the taxonomy, and any change to the names or to the `(module, error_type)`
pairs must be versioned and reflected in Front A's enum/validation before new
trajectories are published.

## Ground truth and diagnosis output

These are different artifacts:

- `failure_annotation` is the scenario's deterministic ground truth, produced by
  Front A for evaluation;
- `TrajectoryDiagnosis` is Front B's prediction, with `outcome`,
  `critical_error` and `feedback`.

Each case's `expected-diagnosis.json` file is a **comparison oracle**, not a
complete copy of `TrajectoryDiagnosis`. The comparator must apply this mapping:

| Case oracle | `TrajectoryDiagnosis` |
| --- | --- |
| `expected_outcome` | `outcome` |
| `critical_error.{step,module,error_type,evidence}` | `critical_error.{step,module,error_type,evidence}` |
| `expected_feedback.target_step` | `feedback.target_step` |
| `expected_feedback.expected_effect` | `feedback.expected_effect` |

The name `expected_effect` deliberately matches Front B's contract. The actual
operational text stays in `feedback.step_instruction`.

## Availability for the paper's metrics

| Metric | Required data | Current status |
| --- | --- | --- |
| Step Accuracy (`S`) | expected step × `critical_error.step` | Available. |
| Step + Module (`S+M`) | expected × diagnosed step and module | Available. |
| All Correct (`ALL`) | expected × diagnosed step, module and type | Available. |
| Task Success Rate | `success` of each execution/re-rollout | Available per execution. |
| Accumulative Success Rate | attempt order and cumulative success from 1 to 5 | Pending re-rollout and the link between attempts. |
| Relative improvement | baseline and paired re-rollouts for the same task | Pending the same link. |

The minimal trajectory contract is enough for the localization metrics, but not
for the recovery metrics on its own. For those, the future rollout contract must
carry at least the root execution identifier, the attempt number and the
resumption point.

For the v2 controls and the fixed 300-sample campaign, `labels[id] = null` means
no expected critical failure, and a missing ID is an integrity error. Blind
evaluation must hide `success` and the labels from the detector; see the
[control protocol](../dataset-v2-success-controls.md#blind-evaluation).
