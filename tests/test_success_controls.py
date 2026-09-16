import hashlib
import json
from uuid import UUID
from zipfile import ZipFile

import pytest

from scripts.build_success_controls import (
    build,
    json_bytes,
    randomize_release,
    shuffle_members,
    validate_control,
)


@pytest.fixture
def control():
    payload = {
        "questions": [
            {
                "text": "Explique testes de APIs.",
                "type": "LONG_TEXT",
                "weight": 1,
                "required": True,
                "rationale": "Avaliar experiência com APIs.",
            }
        ]
    }
    actions = [
        {"tool": "get_info_vaga", "arguments": {"code": "job-1"}},
        {
            "tool": "salvar_formulario",
            "arguments": {
                "questionnaireId": "form-1",
                "payload": payload,
            },
        },
    ]
    results = [{"id": "job-1"}, {"ok": True, "questionnaireId": "form-1", "totalQuestions": 1}]
    steps, messages = [], []
    for index, (action, result) in enumerate(zip(actions, results, strict=True), 1):
        steps.append(
            {
                "index": index,
                "module_outputs": {"planning": "Executar a etapa.", "action": json.dumps(action)},
                "step_input": "Gerar questionário.",
                "env_response": json.dumps({"tool": action["tool"], "result": str(result)}),
                "raw_output": json.dumps({"planning": "Executar a etapa.", "action": action}),
            }
        )
        messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": str(index), "name": action["tool"], "arguments": action["arguments"]}
                    ],
                },
                {
                    "role": "tool",
                    "name": action["tool"],
                    "tool_call_id": str(index),
                    "content": str(result),
                },
            ]
        )
    return {
        "trajectory_id": "baseline-1",
        "task_description": "Gerar questionário de APIs.",
        "environment": "test",
        "success": True,
        "steps": steps,
        "messages": messages,
    }


def test_valid_control_uses_full_messages_for_truncated_lookup(control):
    control["steps"][0]["env_response"] = '{"result": "truncated'
    validate_control(control)


@pytest.mark.parametrize(
    "defect", ["tool_error", "wrong_job", "wrong_count", "wrong_call", "retry"]
)
def test_rejects_success_flag_with_observed_failure(control, defect):
    if defect == "tool_error":
        control["messages"][1]["content"] = '{"error": "VAGA_NOT_FOUND"}'
    elif defect == "wrong_job":
        control["messages"][1]["content"] = '{"id": "another-job"}'
    elif defect == "wrong_count":
        control["messages"][3]["content"] = (
            '{"ok":true,"questionnaireId":"form-1","totalQuestions":2}'
        )
    elif defect == "wrong_call":
        control["messages"][1]["tool_call_id"] = "other-call"
    else:
        control["steps"].append({**control["steps"][-1], "index": 3})
    with pytest.raises(ValueError):
        validate_control(control)


def test_build_preserves_case_content_and_remaps_labels(tmp_path, control):  # noqa: PLR0915 - integração de preservação, revisão e substituição
    dataset = tmp_path / "source"
    baseline_dir = dataset / "private/baseline-trajectories"
    baseline_dir.mkdir(parents=True)
    (baseline_dir / "baseline-1.json").write_bytes(json_bytes(control))
    failed = {k: v for k, v in control.items() if k != "messages"}
    failed.update(trajectory_id="generation-0001", success=False)
    line = json.dumps(failed).encode() + b"\n"
    individual = json_bytes(failed)
    (dataset / "front-b-input.jsonl").write_bytes(line)
    provenance = {
        "trajectory_id": "generation-0001",
        "parent_trajectory_id": "baseline-1",
        "scenario_id": "scenario-1",
        "theme": "software",
        "seniority": "senior",
        "fault_id": "invalid_action",
    }
    (dataset / "private/provenance.jsonl").write_text(json.dumps(provenance) + "\n")
    source_zip = tmp_path / "inputs.zip"
    with ZipFile(source_zip, "w") as z:
        z.writestr("front-b-input.jsonl", line)
        z.writestr("front-b-inputs/generation-0001.json", individual)
    labels = {"generation-0001": {"step": 2, "module": "action", "error_type": "invalid_action"}}
    label_path = tmp_path / "labels.json"
    label_path.write_bytes(json_bytes(labels))
    output = tmp_path / "v2"
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    audit_path = audit_dir / "baseline-1.json"
    review = {
        "accepted": True,
        "issues": [],
        "source_sha256": hashlib.sha256(json_bytes(control)).hexdigest(),
    }
    audit_path.write_bytes(json_bytes(review))
    summary = build(source_zip, label_path, dataset, output, per_group=1, audit_dir=audit_dir)
    assert summary["successful_controls"] == 1
    id_map = json.loads((output / "private/id-map.json").read_bytes())
    with ZipFile(output / "inputs.zip") as z:
        remapped = json.loads(z.read(f"front-b-inputs/{id_map['generation-0001']}.json"))
        assert remapped == {**failed, "trajectory_id": id_map["generation-0001"]}
        clean = json.loads(z.read(f"front-b-inputs/{id_map['generation-0002']}.json"))
        assert clean["success"] is True
        assert clean["steps"] == control["steps"]
        assert "messages" not in clean
        assert "labels.json" not in z.namelist()
    assert json.loads((output / "labels.json").read_bytes()) == {
        id_map["generation-0001"]: labels["generation-0001"],
        id_map["generation-0002"]: None,
    }
    assert json.loads(label_path.read_bytes()) == labels
    with pytest.raises(ValueError, match="diretório de saída novo"):
        build(source_zip, label_path, dataset, output, per_group=1, audit_dir=audit_dir)
    audit_path.write_bytes(
        json_bytes({**review, "accepted": False, "issues": ["Pedido descumprido."]})
    )
    with pytest.raises(ValueError, match="insuficientes"):
        build(
            source_zip, label_path, dataset, tmp_path / "rejected", per_group=1, audit_dir=audit_dir
        )
    supplemental = tmp_path / "supplemental"
    (supplemental / "private/baseline-trajectories").mkdir(parents=True)
    fresh = {**control, "trajectory_id": "baseline-2"}
    (supplemental / "private/baseline-trajectories/baseline-2.json").write_bytes(json_bytes(fresh))
    (supplemental / "private/provenance.jsonl").write_text(
        json.dumps(
            {
                **provenance,
                "trajectory_id": "baseline-2",
                "parent_trajectory_id": "baseline-2",
                "generation_source": "fresh_run",
            }
        )
        + "\n"
    )
    (audit_dir / "baseline-2.json").write_bytes(
        json_bytes({**review, "source_sha256": hashlib.sha256(json_bytes(fresh)).hexdigest()})
    )
    replaced = tmp_path / "replaced"
    replacement_summary = build(
        source_zip,
        label_path,
        dataset,
        replaced,
        per_group=1,
        audit_dir=audit_dir,
        supplemental_dir=supplemental,
    )
    assert replacement_summary["controls_by_source"] == {"fresh_run": 1}
    replacement_provenance = [
        json.loads(line)
        for line in (replaced / "private/provenance.jsonl").read_text().splitlines()
    ]
    assert any(row["parent_trajectory_id"] == "baseline-2" for row in replacement_provenance)
    audit_path.write_bytes(json_bytes({**review, "source_sha256": "stale"}))
    with pytest.raises(ValueError, match="mudou após"):
        build(source_zip, label_path, dataset, tmp_path / "stale", per_group=1, audit_dir=audit_dir)
    label_path.write_text("{}")
    with pytest.raises(ValueError, match="labels"):
        build(
            source_zip, label_path, dataset, tmp_path / "invalid", per_group=1, audit_dir=audit_dir
        )
    assert not (tmp_path / "invalid").exists()


def test_shuffle_preserves_cases_and_aligns_archive_order():
    failures = 10
    rows = [{"trajectory_id": str(i), "success": i >= failures} for i in range(20)]
    lines = [json.dumps(row).encode() for row in rows]
    members = {"front-b-input.jsonl": b"\n".join(lines) + b"\n"}
    members.update({f"front-b-inputs/{row['trajectory_id']}.json": json_bytes(row) for row in rows})
    shuffled = shuffle_members(members, 20260907)
    assert shuffled == shuffle_members(members, 20260907)
    actual = shuffled["front-b-input.jsonl"].splitlines()
    assert actual != lines
    assert set(actual) == set(lines)
    assert list(shuffled)[2:] == [
        f"front-b-inputs/{json.loads(line)['trajectory_id']}.json" for line in actual
    ]
    for name in list(shuffled)[2:]:
        assert shuffled[name] == members[name]


def test_randomize_release_aligns_ids_order_and_labels():
    uuid_version = 4
    rows = [{"trajectory_id": f"generation-{i}", "success": i % 2 == 0} for i in range(20)]
    labels = {r["trajectory_id"]: None if r["success"] else {"step": 1} for r in rows}
    provenance = [
        {"trajectory_id": r["trajectory_id"], "parent_trajectory_id": f"parent-{i}"}
        for i, r in enumerate(rows)
    ]
    members = {"front-b-input.jsonl": b"".join(json.dumps(r).encode() + b"\n" for r in rows)}
    members.update({f"front-b-inputs/{r['trajectory_id']}.json": json_bytes(r) for r in rows})
    shuffled, new_labels, new_provenance, id_map = randomize_release(members, labels, provenance, 1)
    new_rows = [json.loads(line) for line in shuffled["front-b-input.jsonl"].splitlines()]
    ids = [r["trajectory_id"] for r in new_rows]
    assert len(set(ids)) == len(rows)
    assert set(ids).isdisjoint(labels)
    assert all(UUID(tid.removeprefix("trajectory-")).version == uuid_version for tid in ids)
    assert list(new_labels) == ids == [p["trajectory_id"] for p in new_provenance]
    assert list(shuffled)[2:] == [f"front-b-inputs/{tid}.json" for tid in ids]
    assert set(shuffled) == {"front-b-input.jsonl", "front-b-inputs/"} | {
        f"front-b-inputs/{tid}.json" for tid in ids
    }
    for old, parent in zip(rows, provenance, strict=True):
        tid = id_map[old["trajectory_id"]]
        assert new_labels[tid] == labels[old["trajectory_id"]]
        assert json.loads(shuffled[f"front-b-inputs/{tid}.json"]) == {**old, "trajectory_id": tid}
        assert next(p for p in new_provenance if p["trajectory_id"] == tid) == {
            **parent,
            "trajectory_id": tid,
            "case_id": tid,
        }
    assert list(labels) == [r["trajectory_id"] for r in rows]
