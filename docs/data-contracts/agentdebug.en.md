# Scenario Emulator → AgentDebug-RH data contract

[🇧🇷 Português](agentdebug.md) · 🇺🇸 **English** · [Home](../../README.en.md)

Front A's executable schema is `src/schemas/agent_debug/schema.py`; Front B's
corresponding consumer is `src/schemas/observability/execution.py` in AgentDebug-RH.

## Execution unit: step

A step is one logical agent decision cycle, not an atomic infrastructure operation:

```text
step context → memory/reflection when emitted → planning → action → env_response
```

Planning, tool call, and response belong to the same step. The next starts when
the agent decides again with the previous environmental response available.
Single-inference generators/evaluators use one step for the complete inference.
Indices are contiguous and 1-indexed. Do not invent memory/reflection for step 1.

## Front B input

```json
{
  "trajectory_id": "exec-001",
  "task_description": "Generate a recruitment questionnaire",
  "environment": "recruitment system",
  "success": false,
  "steps": [{
    "index": 1,
    "module_outputs": {
      "planning": "Look up the job",
      "action": "{\"tool\":\"get_info_vaga\",\"arguments\":{}}"
    },
    "step_input": "Context available before this decision",
    "env_response": "Tool response",
    "raw_output": "{\"planning\":\"Look up the job\",\"action\":{\"tool\":\"get_info_vaga\",\"arguments\":{}}}"
  }]
}
```

| Trajectory field | Meaning |
|---|---|
| `trajectory_id` | Correlation identifier for execution, diagnosis, and trace |
| `task_description` | Objective used to assess behavior |
| `environment` | Producing agent/environment |
| `success` | Scenario oracle outcome, not predicted diagnosis |
| `steps` | Ordered decision cycles |

| Step field | Meaning |
|---|---|
| `index` | Contiguous position starting at 1 |
| `module_outputs` | Observable outputs by module; missing modules are not fabricated |
| `step_input` | Context before the decision |
| `env_response` | Environment result after that step's action |
| `raw_output` | Complete observable output from which modules were extracted |

When the SDK exposes full model output, preserve it. When streaming splits planning
and tool calls, preserve a complete JSON envelope of observable fragments. Do not
repeat only action if planning was also extracted. Output may be empty only when
execution failed before producing any agent output. Never truncate an envelope
mid-JSON, including large action payloads.

Questionnaire trajectories may additionally carry normalized `messages` for prefix
handoff. Campaign public exports deliberately retain only the five top-level
fields above; private originals retain richer context.

## Discovering analyzable modules

Front B reads nonempty `module_outputs` keys; there is no parallel module list.
Taxonomy names are `memory`, `reflection`, `planning`, `action`, `system`, `others`.
The documented fine-analysis stage handles the first four; system causes are
inferred from environment evidence at root-cause selection; others is a fallback.
Coordinate taxonomy changes between producer and consumer before publishing data.

## Ground truth and diagnosis output

`failure_annotation` is deterministic experimental ground truth from Front A.
`TrajectoryDiagnosis` is Front B's prediction with `outcome`, `critical_error`,
and `feedback`. Example `expected-diagnosis.json` files are partial comparison
oracles, not full `TrajectoryDiagnosis` dumps.

| Example oracle | Diagnosis |
|---|---|
| `expected_outcome` | `outcome` |
| `critical_error.{step,module,error_type,evidence}` | Same fields under `critical_error` |
| `expected_feedback.target_step` | `feedback.target_step` |
| `expected_feedback.expected_effect` | `feedback.expected_effect` |

Operational correction text remains `feedback.step_instruction`.

## Metrics supported by the contract

| Metric | Required comparison | Availability |
|---|---|---|
| Step Accuracy (`S`) | Expected vs predicted critical step | Available |
| Step + Module (`S+M`) | Step and module | Available |
| All Correct (`ALL`) | Step, module, and error type | Available |
| Task Success Rate | Execution/re-rollout `success` | Per execution |
| Accumulative Success Rate | Ordered attempts and cumulative success from 1 to 5 | Requires re-rollout and attempt lineage |
| Relative improvement | Paired baseline and re-rollouts | Requires the same lineage |

Localization does not require replay, but recovery metrics do. Recovery lineage
must identify root execution, attempt number, and resumption point. For v2
controls, `labels[id] = null` means no expected critical error; missing IDs are
integrity errors. Blind evaluation must hide success/labels from the detector;
see the [control protocol](../dataset-v2-success-controls.en.md).
