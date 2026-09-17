"""Acrescenta controles, anonimiza IDs e embaralha dados e labels em conjunto."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from src.schemas.dataset.schema import DatasetLabel, FrontBTrajectory
from src.services.dataset.controls import (
    parse_response as parse_response,  # noqa: PLC0414 - compatibilidade dos imports existentes
)
from src.services.dataset.controls import validate_control


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def shuffle_members(members: dict[str, bytes], seed: int) -> dict[str, bytes]:
    """Mistura linhas e entradas do ZIP preservando IDs e conteúdo de cada caso."""
    lines = [line for line in members["front-b-input.jsonl"].splitlines() if line.strip()]
    random.Random(seed).shuffle(lines)
    shuffled = {"front-b-input.jsonl": b"\n".join(lines) + b"\n", "front-b-inputs/": b""}
    for line in lines:
        tid = json.loads(line)["trajectory_id"]
        name = f"front-b-inputs/{tid}.json"
        shuffled[name] = members[name]
    return shuffled


def randomize_release(
    members: dict[str, bytes], labels: dict, provenance: list[dict], seed: int
) -> tuple[dict, dict, list[dict], dict]:
    """Substitui IDs por UUIDs aleatórios e sincroniza a ordem de todos os artefatos."""
    rows = [
        json.loads(line) for line in members["front-b-input.jsonl"].splitlines() if line.strip()
    ]
    ids = [row["trajectory_id"] for row in rows]
    provenance_by_id = {row["trajectory_id"]: row for row in provenance}
    if (
        len(set(ids)) != len(ids)
        or set(ids) != set(labels)
        or len(provenance_by_id) != len(provenance)
        or set(ids) != set(provenance_by_id)
    ):
        raise ValueError("Dados, labels e proveniência precisam ter IDs únicos correspondentes.")
    id_map = {tid: f"trajectory-{uuid4()}" for tid in ids}
    if len(set(id_map.values())) != len(ids):
        raise ValueError("Colisão de identificadores aleatórios.")
    renamed = {}
    for row in rows:
        old_id = row["trajectory_id"]
        if json.loads(members[f"front-b-inputs/{old_id}.json"]) != row:
            raise ValueError("JSON individual diverge do JSONL antes da troca de IDs.")
        row["trajectory_id"] = id_map[old_id]
        renamed[f"front-b-inputs/{row['trajectory_id']}.json"] = json_bytes(row)
    renamed["front-b-input.jsonl"] = b"".join(
        (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows
    )
    shuffled = shuffle_members(renamed, seed)
    inverse = {new: old for old, new in id_map.items()}
    order = [
        json.loads(line)["trajectory_id"] for line in shuffled["front-b-input.jsonl"].splitlines()
    ]
    shuffled_labels = {tid: labels[inverse[tid]] for tid in order}
    shuffled_provenance = [
        {**provenance_by_id[inverse[tid]], "trajectory_id": tid, "case_id": tid} for tid in order
    ]
    return shuffled, shuffled_labels, shuffled_provenance, id_map


def load_source(source_zip: Path, labels_path: Path) -> tuple[dict, list, dict]:
    labels = json.loads(labels_path.read_bytes())
    with ZipFile(source_zip) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    if len(members) != len(archive.namelist()):
        raise ValueError("ZIP contém nomes duplicados.")
    original_jsonl = members["front-b-input.jsonl"]
    original = [json.loads(line) for line in original_jsonl.splitlines() if line.strip()]
    ids = [row["trajectory_id"] for row in original]
    if len(set(ids)) != len(ids) or set(ids) != set(labels):
        raise ValueError("IDs duplicados ou labels sem correspondência exata.")
    for row in original:
        FrontBTrajectory.model_validate(row)
        DatasetLabel.model_validate(labels[row["trajectory_id"]])
        if row["success"] is not False:
            raise ValueError("Fonte deve conter apenas a versão original com falhas.")
        name = f"front-b-inputs/{row['trajectory_id']}.json"
        if json.loads(members[name]) != row:
            raise ValueError("JSON individual diverge do JSONL.")
    expected_members = {"front-b-input.jsonl", "front-b-inputs/"} | {
        f"front-b-inputs/{tid}.json" for tid in ids
    }
    if set(members) - expected_members:
        raise ValueError("ZIP contém arquivos fora do contrato público.")
    return members, original, labels


def select_controls(
    baseline_dataset: Path,
    original: list,
    per_group: int,
    audit_dir: Path,
    supplemental_dir: Path | None = None,
) -> tuple:
    ids = [row["trajectory_id"] for row in original]
    local_rows = [
        json.loads(line)
        for line in (baseline_dataset / "front-b-input.jsonl").read_text().splitlines()
    ]
    if {r["trajectory_id"]: r for r in local_rows} != dict(zip(ids, original, strict=True)):
        raise ValueError("Baselines locais não correspondem ao dataset de origem.")
    provenance = [
        json.loads(line)
        for line in (baseline_dataset / "private/provenance.jsonl").read_text().splitlines()
    ]
    if {r["trajectory_id"] for r in provenance} != set(ids):
        raise ValueError("Proveniência incompleta.")
    parents = {r["parent_trajectory_id"]: r for r in provenance}
    expected_groups = {(r["theme"], r["seniority"]) for r in provenance}
    sources = {
        parent: baseline_dataset / "private/baseline-trajectories" / f"{parent}.json"
        for parent in parents
    }
    if supplemental_dir is not None:
        for line in (supplemental_dir / "private/provenance.jsonl").read_text().splitlines():
            candidate = json.loads(line)
            parent = candidate["parent_trajectory_id"]
            if (
                parent in parents
                or (candidate["theme"], candidate["seniority"]) not in expected_groups
            ):
                raise ValueError("Candidato suplementar duplicado ou de cenário desconhecido.")
            parents[parent] = candidate
            sources[parent] = supplemental_dir / "private/baseline-trajectories" / f"{parent}.json"
    groups = defaultdict(list)
    rejected = []
    for parent, truth in sorted(parents.items()):
        source = sources[parent]
        data = json.loads(source.read_text())
        if data["trajectory_id"] != parent:
            raise ValueError("ID da baseline diverge da proveniência.")
        try:
            validate_control(data)
        except (ValueError, KeyError, TypeError, SyntaxError) as exc:
            rejected.append({"parent_trajectory_id": parent, "reason": str(exc)})
            continue
        audit_path = audit_dir / f"{parent}.json"
        if not audit_path.exists():
            rejected.append(
                {"parent_trajectory_id": parent, "reason": "Sem revisão das instruções."}
            )
            continue
        audit = json.loads(audit_path.read_text())
        if audit.get("source_sha256") != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError("Baseline mudou após a revisão de instruções.")
        if audit.get("accepted") is not True or audit.get("issues") or audit.get("audit_error"):
            rejected.append(
                {
                    "parent_trajectory_id": parent,
                    "reason": "Reprovado na revisão de instruções.",
                    "issues": audit.get("issues", []),
                }
            )
            continue
        groups[(truth["theme"], truth["seniority"])].append((data, truth))
    if set(groups) != expected_groups or any(len(v) < per_group for v in groups.values()):
        raise ValueError("Baselines limpas insuficientes para balancear os cenários.")
    groups = {
        group: sorted(
            candidates,
            key=lambda item: (
                item[1].get("generation_source") == "fresh_run",
                item[0]["trajectory_id"],
            ),
        )
        for group, candidates in groups.items()
    }
    return groups, rejected, provenance


def build(
    source_zip: Path,
    labels_path: Path,
    baseline_dataset: Path,
    output_dir: Path,
    per_group: int = 10,
    *,
    audit_dir: Path,
    supplemental_dir: Path | None = None,
    shuffle_seed: int = 20260907,
) -> dict:
    if per_group < 1:
        raise ValueError("per_group deve ser positivo.")
    if output_dir.exists():
        raise ValueError("Use um diretório de saída novo para preservar versões anteriores.")
    members, original, labels = load_source(source_zip, labels_path)
    groups, rejected, provenance = select_controls(
        baseline_dataset, original, per_group, audit_dir, supplemental_dir
    )
    original_jsonl = members["front-b-input.jsonl"]
    additions = []
    next_id = 1
    used_ids = set(labels)
    for group in sorted(groups):
        for data, truth in groups[group][:per_group]:
            while f"generation-{next_id:04d}" in used_ids:
                next_id += 1
            tid = f"generation-{next_id:04d}"
            used_ids.add(tid)
            row = {k: v for k, v in data.items() if k != "messages"}
            row["trajectory_id"] = tid
            FrontBTrajectory.model_validate(row)
            labels[tid] = None
            additions.append(row)
            members[f"front-b-inputs/{tid}.json"] = json_bytes(row)
            provenance.append(
                {
                    "case_id": tid,
                    "trajectory_id": tid,
                    "parent_trajectory_id": data["trajectory_id"],
                    "scenario_id": truth["scenario_id"],
                    "theme": group[0],
                    "seniority": group[1],
                    "fault_id": None,
                    "injection": None,
                    "validation": "operational_checks_and_instruction_review_passed",
                    "generation_source": truth.get("generation_source", "original_baseline"),
                }
            )
    separator = b"" if original_jsonl.endswith(b"\n") else b"\n"
    members["front-b-input.jsonl"] = (
        original_jsonl
        + separator
        + b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in additions)
    )
    members, labels, provenance, id_map = randomize_release(
        members, labels, provenance, shuffle_seed
    )
    summary = {
        "schema_version": "2.0",
        "original_cases": len(original),
        "successful_controls": len(additions),
        "total_cases": len(labels),
        "eligible_baselines": sum(map(len, groups.values())),
        "rejected_baselines": rejected,
        "controls_per_group": {"/".join(k): per_group for k in sorted(groups)},
        "fault_counts": dict(Counter(r["fault_id"] for r in provenance if r["fault_id"])),
        "source_sha256": {
            "inputs.zip": hashlib.sha256(source_zip.read_bytes()).hexdigest(),
            "labels.json": hashlib.sha256(labels_path.read_bytes()).hexdigest(),
        },
        "negative_label": None,
        "shuffle_seed": shuffle_seed,
        "public_id_scheme": "random_uuid4",
        "labels_order": "same_as_jsonl_and_zip_members",
        "instruction_review_required": True,
        "controls_by_source": dict(
            Counter(r["generation_source"] for r in provenance if r["fault_id"] is None)
        ),
        "limitations": [
            "Controles aprovados nas verificações operacionais e na revisão de instruções; revisão não é prova formal de ausência de todo erro.",
            "Respostas de lookup nos steps originais podem estar truncadas; validação usa messages completos privados.",
            "Detector deve analisar todos os casos sem acesso a success/labels ou a regras que pulem sucessos.",
            "Agrupar controles e falhas derivadas por parent_trajectory_id ao criar splits.",
        ],
    }
    output_dir.mkdir(parents=True)
    (output_dir / "private").mkdir()
    with ZipFile(output_dir / "inputs.zip", "w", ZIP_DEFLATED) as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
    (output_dir / "labels.json").write_bytes(json_bytes(labels))
    (output_dir / "private/manifest.json").write_bytes(json_bytes(summary))
    (output_dir / "private/id-map.json").write_bytes(json_bytes(id_map))
    reviewed_parents = {r["parent_trajectory_id"] for r in provenance if r["fault_id"] is None}
    (output_dir / "private/control-audit.jsonl").write_text(
        "".join(
            json.dumps(json.loads((audit_dir / f"{parent}.json").read_text()), ensure_ascii=False)
            + "\n"
            for parent in sorted(reviewed_parents)
        )
    )
    (output_dir / "private/provenance.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in provenance)
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--baseline-dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--per-group", type=int, default=10)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--supplemental-dir", type=Path)
    parser.add_argument("--shuffle-seed", type=int, default=20260907)
    args = parser.parse_args()
    summary = build(
        args.source_zip,
        args.labels,
        args.baseline_dataset,
        args.output_dir,
        args.per_group,
        audit_dir=args.audit_dir,
        supplemental_dir=args.supplemental_dir,
        shuffle_seed=args.shuffle_seed,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
