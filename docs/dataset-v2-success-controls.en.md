# Dataset v2: 1,100 failures + 100 successful controls

[🇧🇷 Português](dataset-v2-success-controls.md) · 🇺🇸 **English** · [Home](../README.en.md)

This document translates the locally recorded v2 preparation protocol. Historical
counts below describe that preparation, not a campaign rerun during repository
separation. The source artifacts are not included in a clean clone.

## Composition and selection

The v2 release preserves the contents and labels of 1,100 previous failure cases
(100 per implemented class), adds 100 reviewed real trajectories without fault
injection (ten per scenario/seniority combination across ten scenarios), and
replaces all public IDs with random UUIDs. Total: 1,200 inputs and 1,200 label entries.
The recorded final controls comprise 87 original baselines and 13 fresh executions.

Of 220 original baselines reviewed, 107 passed operational and instruction checks.
Approval requires interpreting the coordinator's actual request, not only its
count metadata: one requested question answered with eight is rejected even if
`success=true`. Tool errors, retries, missed focus, missing required fields,
inappropriate types, and instruction-violating weights are also rejected.

Fresh controls use the real `QuestionnaireService`, OpenAI `gpt-5-mini`, local
versioned prompts, and explicit counts in both command and metadata. Questions
are not edited after generation to force approval. Private `generation_source`
distinguishes `original_baseline` and `fresh_run`.

Each control has exactly two steps: `get_info_vaga`, then `salvar_formulario`.
Deterministic checks verify schema, call/response correspondence, arguments,
job/form IDs, persisted question count, and success confirmation. Full private
`messages` verify tool responses because historical lookup `env_response` values
may be truncated.

A separate `gpt-5-mini` call reviews the command, system instructions, job, full
tool calls/responses, and complete questions. It checks count, focus, types,
weights, and remaining constraints without seeing the expected label. The source
protocol records 99 API-approved controls and one approved by direct full-content
review during the correction session, plus direct sample inspection. Each case's
method is recorded in its audit.

These reviews are not formal proof of error absence. Reviewer and generator use
the same model family and may share limitations. Report ground-truth origin in
metrics. `private/control-audit.jsonl` stores audits and source hashes; export
rejects unapproved controls or content changed after review.

## Release files

Publish together with new versioned names, retaining the previous release:

1. `error-recovery-front-b-v2-1200-inputs.zip`: shuffled JSONL and 1,200 individual JSONs.
2. `error-recovery-front-b-v2-1200-labels.json`: complete ID-to-label map.
3. The v2 composition and evaluation guide.

The ZIP contains only `front-b-input.jsonl` and `front-b-inputs/`. Labels stay
outside the ZIP and are evaluation-only. Full provenance remains in the local
package's `private/provenance.jsonl`, not detector input. Download matching inputs
and labels together: previous release IDs do not match the new IDs.

## v2 labels

```json
{
  "trajectory-c7d8ed29-7382-45ad-86d0-9c643371005c": null,
  "trajectory-3414d18d-b535-4118-8dc1-2b6b8cb5e28c": {
    "step": 2, "module": "system", "error_type": "environment_error"
  }
}
```

`null` means **no expected critical error**, not missing label. Do not convert it
to `others`, step zero, or a new taxonomy class. Readers must accept
`DatasetLabel | None`; an absent ID is an integrity error. IDs above are illustrative.
Labels, JSONL records, and ZIP entries share export order, but always correlate
by ID because readers/extractors can reorder them.

## Blind evaluation

The Front B behavior documented during preparation skips `success=true` in both
the main pipeline and root-cause detection. Simply appending controls does not
measure false positives. A blind mode must:

1. Run detection on every trajectory, including controls.
2. Hide `success`, labels, provenance, and ID maps from models and from skip logic.
   Removing `success` is insufficient if its default still assumes failure.
3. Explicitly allow “no error” in prompts and root-cause flow.
4. Use IDs only to join predictions and labels after analysis.
5. Count FP for a control predicted as an error and TN for a valid “no error”
   prediction. Compute `FPR = FP / (FP + TN)`; with 100 valid control predictions,
   the denominator is 100. Report runtime failures and coverage separately;
   timeouts/invalid output are not TN.

Predefine whether module-detector alerts or final critical diagnoses are measured;
if both, report separate rates. S, S+M, and ALL apply only to labeled failures.
No false-positive measurement was performed in preparing this package.

Some controls reuse baselines underlying failure cases. Keep each control and
all derivatives with the same `parent_trajectory_id` in the same split. The 1,200
inputs/labels/ZIP entries share permutation seed `20260907`. Public UUID v4 IDs
are generated independently of class, with no old/control numeric ranges. Only
`private/id-map.json` maps old to new IDs. Provenance retains baseline ancestry.

## Local reproduction

The original ZIP and labels were downloaded from Drive and compared byte-for-byte
with the local 1,100-case export, according to the source preparation record.
Recorded SHA-256 values:

- ZIP: `b496cd31d6d0932722ac806db04db9d774b2005b061190ba21821bc19c5d0051`.
- Labels: `53c8c95dc7193474ef4f66e3ba8422942846a815fc9ad92c782b3c4707af62d1`.

From this repository root, with the required historical artifacts available,
choose a fresh destination:

```bash
uv run python -m scripts.build_success_controls \
  --source-zip outputs/drive-original-20260907/inputs.zip \
  --labels outputs/drive-original-20260907/labels.json \
  --baseline-dataset outputs/error-recovery-front-b-1100 \
  --audit-dir outputs/control-audit-v2-20260907 \
  --supplemental-dir outputs/control-replacements-20260907 \
  --output-dir outputs/error-recovery-front-b-v2-1200-reviewed \
  --per-group 10 --shuffle-seed 20260907
```

This writes `inputs.zip`, `labels.json`, `private/manifest.json`,
`private/provenance.jsonl`, `private/control-audit.jsonl`, and `private/id-map.json`.
Each export generates new random UUIDs: retain the package/private map for exact
identity reproduction. The manifest records composition, exclusions, source
checksums, and limitations. Existing destinations and incompatible sources are rejected.

`scripts/audit_success_controls.py` and `scripts/generate_success_replacements.py`
record review and fresh generation separately. Audit both original and replacement
baselines before export. Individual `front-b-inputs/` JSONs contain full public
contract trajectories and saved payloads, not Markdown summaries.
