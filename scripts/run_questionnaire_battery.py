from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from src.agents.model import configured_model_identifier
from src.clients.langfuse.client import flush_langfuse
from src.schemas.coordinator_prompt.schema import (
    CoordinatorPrompt,
    ExpectedAction,
    PromptCategory,
    PromptIntent,
)
from src.schemas.job_description.schema import JobDescription
from src.services.questionnaire.service import QuestionnaireService

EXPECTED_JOB_COUNT = 5
EXPECTED_THEME_COUNT = 15
EXPECTED_LEVELS = {1, 2, 3, 4, 5}
DATA_DICTIONARY_FILENAME = "DATA_DICTIONARY.md"


@dataclass(frozen=True)
class WorkItem:
    job: JobDescription
    case_id: str
    theme_id: str | None
    theme_name: str | None
    attack_level: int | None
    strategy: str
    is_control: bool
    command: str
    category: PromptCategory
    rationale: str
    sequence: int
    repetition: int

    @property
    def run_key(self) -> str:
        return f"{self.job.id}:{self.case_id}:r{self.repetition:02d}"


class BatterySQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaigns (
                campaign TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                model TEXT NOT NULL,
                planned_generations INTEGER NOT NULL,
                evaluation_enabled INTEGER NOT NULL,
                classification_enabled INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                manifest_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                campaign TEXT NOT NULL,
                job_id TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (campaign, job_id),
                FOREIGN KEY (campaign) REFERENCES campaigns(campaign) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS questionnaire_cases (
                campaign TEXT NOT NULL,
                case_id TEXT NOT NULL,
                is_control INTEGER NOT NULL,
                theme_id TEXT,
                theme_name TEXT,
                attack_level INTEGER,
                strategy TEXT NOT NULL,
                prompt_intent TEXT NOT NULL,
                prompt_category TEXT NOT NULL,
                expected_action TEXT NOT NULL,
                prompt TEXT NOT NULL,
                PRIMARY KEY (campaign, case_id),
                FOREIGN KEY (campaign) REFERENCES campaigns(campaign) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS generations (
                run_key TEXT PRIMARY KEY,
                campaign TEXT NOT NULL,
                job_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                repetition INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                duration_ms INTEGER,
                trajectory_id TEXT,
                trace_id TEXT,
                failure_reason TEXT,
                questionnaire_json TEXT,
                record_json TEXT NOT NULL,
                FOREIGN KEY (campaign, job_id) REFERENCES jobs(campaign, job_id),
                FOREIGN KEY (campaign, case_id)
                    REFERENCES questionnaire_cases(campaign, case_id)
            );

            CREATE INDEX IF NOT EXISTS idx_battery_generations_job
                ON generations(campaign, job_id);
            CREATE INDEX IF NOT EXISTS idx_battery_generations_case
                ON generations(campaign, case_id);
            CREATE INDEX IF NOT EXISTS idx_battery_generations_status
                ON generations(campaign, status);
            """
        )

    def initialize(self, manifest: dict[str, Any], work: list[WorkItem]) -> None:
        now = datetime.now(UTC).isoformat()
        campaign = manifest["campaign"]
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO campaigns (
                    campaign, schema_version, model, planned_generations,
                    evaluation_enabled, classification_enabled, created_at,
                    updated_at, manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign) DO UPDATE SET
                    model = excluded.model,
                    planned_generations = excluded.planned_generations,
                    evaluation_enabled = excluded.evaluation_enabled,
                    classification_enabled = excluded.classification_enabled,
                    updated_at = excluded.updated_at,
                    manifest_json = excluded.manifest_json
                """,
                (
                    campaign,
                    manifest["schema_version"],
                    manifest["model"],
                    manifest["planned_generations"],
                    int(manifest["evaluation_enabled"]),
                    int(manifest["classification_enabled"]),
                    manifest["created_at"],
                    now,
                    json.dumps(manifest, ensure_ascii=False),
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO jobs (campaign, job_id, title, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(campaign, job_id) DO UPDATE SET
                    title = excluded.title,
                    payload_json = excluded.payload_json
                """,
                [
                    (
                        campaign,
                        job["id"],
                        job["title"],
                        json.dumps(job, ensure_ascii=False),
                    )
                    for job in manifest["jobs"]
                ],
            )
            unique_cases = {item.case_id: item for item in work}
            self.connection.executemany(
                """
                INSERT INTO questionnaire_cases (
                    campaign, case_id, is_control, theme_id, theme_name,
                    attack_level, strategy, prompt_intent, prompt_category,
                    expected_action, prompt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign, case_id) DO UPDATE SET
                    is_control = excluded.is_control,
                    theme_id = excluded.theme_id,
                    theme_name = excluded.theme_name,
                    attack_level = excluded.attack_level,
                    strategy = excluded.strategy,
                    prompt_intent = excluded.prompt_intent,
                    prompt_category = excluded.prompt_category,
                    expected_action = excluded.expected_action,
                    prompt = excluded.prompt
                """,
                [
                    (
                        campaign,
                        item.case_id,
                        int(item.is_control),
                        item.theme_id,
                        item.theme_name,
                        item.attack_level,
                        item.strategy,
                        "benign" if item.is_control else "malicious",
                        item.category.value,
                        "comply" if item.is_control else "refuse",
                        item.command,
                    )
                    for item in unique_cases.values()
                ],
            )

    def save_generation(self, record: dict[str, Any]) -> None:
        questionnaire = record.get("questionnaire")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO generations (
                    run_key, campaign, job_id, case_id, repetition, status,
                    started_at, completed_at, duration_ms, trajectory_id, trace_id,
                    failure_reason, questionnaire_json, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    duration_ms = excluded.duration_ms,
                    trajectory_id = excluded.trajectory_id,
                    trace_id = excluded.trace_id,
                    failure_reason = excluded.failure_reason,
                    questionnaire_json = excluded.questionnaire_json,
                    record_json = excluded.record_json
                """,
                (
                    record["run_key"],
                    record["campaign"],
                    record["job_id"],
                    record["case_id"],
                    record["repetition"],
                    record["status"],
                    record["started_at"],
                    record["completed_at"],
                    record.get("duration_ms"),
                    record.get("trajectory_id"),
                    record.get("trace_id"),
                    record.get("failure_reason"),
                    json.dumps(questionnaire, ensure_ascii=False) if questionnaire else None,
                    json.dumps(record, ensure_ascii=False),
                ),
            )

    def close(self) -> None:
        self.connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Executa apenas a geração de questionários da bateria de atributos sensíveis, "
            "sem respostas, avaliador ou classificação dos resultados."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/questionnaire_battery.yaml"),
        help="Corpus YAML com vagas, controle e ataques.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs/questionnaire-battery"),
        help="Diretório de manifest.json e generations.jsonl.",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Repetições por ataque e por controle (default: 1).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Máximo de gerações simultâneas (default: 3).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limita novos itens executados; útil para smoke test.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida e grava o manifesto sem chamar o modelo.",
    )
    return parser


def _load_config(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("A raiz da configuração deve ser um objeto YAML.")
    jobs = raw.get("jobs")
    themes = raw.get("themes")
    control = raw.get("control")
    if not isinstance(jobs, list) or len(jobs) != EXPECTED_JOB_COUNT:
        raise ValueError(f"A bateria exige exatamente {EXPECTED_JOB_COUNT} vagas.")
    if not isinstance(themes, list) or len(themes) != EXPECTED_THEME_COUNT:
        raise ValueError(f"A bateria exige exatamente {EXPECTED_THEME_COUNT} temas.")
    if not isinstance(control, dict) or not str(control.get("command", "")).strip():
        raise ValueError("A bateria exige um comando de controle não vazio.")

    job_ids = [str(item.get("id", "")) for item in jobs if isinstance(item, dict)]
    theme_ids = [str(item.get("id", "")) for item in themes if isinstance(item, dict)]
    if len(set(job_ids)) != EXPECTED_JOB_COUNT:
        raise ValueError("IDs de vagas devem ser únicos e não vazios.")
    if len(set(theme_ids)) != EXPECTED_THEME_COUNT:
        raise ValueError("IDs de temas devem ser únicos e não vazios.")

    for theme in themes:
        levels = theme.get("levels") if isinstance(theme, dict) else None
        if not isinstance(levels, list) or {item.get("level") for item in levels} != EXPECTED_LEVELS:
            raise ValueError(f"Tema {theme.get('id', '<inválido>')} deve conter níveis 1 a 5.")
        for level in levels:
            if not str(level.get("command", "")).strip():
                raise ValueError(
                    f"Tema {theme['id']} nível {level.get('level')} possui comando vazio."
                )
    return raw


def _build_work(config: dict[str, Any], repetitions: int) -> list[WorkItem]:
    if repetitions < 1:
        raise ValueError("--repetitions deve ser maior ou igual a 1.")
    control = config["control"]
    jobs = [JobDescription.model_validate(item) for item in config["jobs"]]
    work: list[WorkItem] = []
    for job in jobs:
        templates: list[dict[str, Any]] = [
            {
                "case_id": "control",
                "theme_id": None,
                "theme_name": None,
                "attack_level": None,
                "strategy": "neutral_control",
                "is_control": True,
                "command": control["command"],
                "category": PromptCategory.PROFESSIONAL_CUSTOMIZATION,
                "rationale": control.get("rationale", "Controle sem injeção adversarial."),
            }
        ]
        for theme in config["themes"]:
            category = PromptCategory(theme["category"])
            for level in sorted(theme["levels"], key=lambda item: item["level"]):
                templates.append(
                    {
                        "case_id": f"{theme['id']}-l{level['level']}",
                        "theme_id": theme["id"],
                        "theme_name": theme["name"],
                        "attack_level": level["level"],
                        "strategy": level["strategy"],
                        "is_control": False,
                        "command": level["command"],
                        "category": category,
                        "rationale": (
                            f"Caso adversarial da categoria {theme['name']}, "
                            f"estratégia {level['strategy']}."
                        ),
                    }
                )
        for sequence, template in enumerate(templates, start=1):
            for repetition in range(1, repetitions + 1):
                work.append(
                    WorkItem(
                        job=job,
                        sequence=sequence,
                        repetition=repetition,
                        **template,
                    )
                )
    return work


def _load_completed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSONL inválido em {path}:{line_number}: {exc}") from exc
        if run_key := record.get("run_key"):
            completed.add(str(run_key))
    return completed


def _manifest(config: dict[str, Any], work: list[WorkItem]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "campaign": config["name"],
        "description": config.get("description"),
        "created_at": datetime.now(UTC).isoformat(),
        "model": configured_model_identifier(),
        "evaluation_enabled": False,
        "classification_enabled": False,
        "job_count": len(config["jobs"]),
        "theme_count": len(config["themes"]),
        "attack_cases_per_job": EXPECTED_THEME_COUNT * len(EXPECTED_LEVELS),
        "control_cases_per_job": 1,
        "repetitions": max(item.repetition for item in work),
        "planned_generations": len(work),
        "jobs": config["jobs"],
        "control": config["control"],
        "themes": config["themes"],
    }


def _write_generation_json(directory: Path, record: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    filename = record["run_key"].replace(":", "__") + ".json"
    path = directory / filename
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_summary(
    output_dir: Path,
    *,
    manifest: dict[str, Any],
    generations_path: Path,
) -> dict[str, Any]:
    records = _records(generations_path)
    counts_by_status: dict[str, int] = {}
    for record in records:
        status = str(record["status"])
        counts_by_status[status] = counts_by_status.get(status, 0) + 1
    summary = {
        "schema_version": "1.0",
        "campaign": manifest["campaign"],
        "updated_at": datetime.now(UTC).isoformat(),
        "planned_generations": manifest["planned_generations"],
        "recorded_generations": len(records),
        "pending_generations": max(0, manifest["planned_generations"] - len(records)),
        "complete": len(records) == manifest["planned_generations"],
        "controls_recorded": sum(bool(record["is_control"]) for record in records),
        "attacks_recorded": sum(not bool(record["is_control"]) for record in records),
        "questionnaires_recorded": sum(record.get("questionnaire") is not None for record in records),
        "questionnaires_absent": sum(record.get("questionnaire") is None for record in records),
        "counts_by_status": counts_by_status,
        "evaluation_enabled": False,
        "classification_enabled": False,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _write_data_dictionary(output_dir: Path, summary: dict[str, Any]) -> None:
    content = f"""# Dicionário de dados — bateria de questionários

Este diretório contém somente a etapa de geração de questionários. Não há respostas de
candidatos, avaliação de segurança nem classificação de 0 a 3. Estado da coleta:
`{summary['recorded_generations']}/{summary['planned_generations']}` gerações registradas;
`complete={str(summary['complete']).lower()}`.

## Arquivos

- `manifest.json`: definição imutável da campanha, das cinco vagas, do controle e dos 75 ataques.
- `generations.jsonl`: um objeto JSON completo por linha e por tentativa de geração.
- `generations/*.json`: os mesmos registros do JSONL, separados em arquivos individuais.
- `summary.json`: contagens operacionais da coleta, sem julgamento do conteúdo.
- `questionnaire_battery.sqlite3`: representação relacional da campanha e cópia integral de cada registro em `record_json`.
- `{DATA_DICTIONARY_FILENAME}`: este dicionário de dados.

## Chaves de `manifest.json`

- `schema_version`: versão do contrato destes artefatos; atualmente `1.0`.
- `campaign`: identificador estável da bateria.
- `description`: objetivo textual da campanha.
- `created_at`: instante de criação do manifesto em ISO 8601 e UTC.
- `model`: identificador do modelo usado pelo agente gerador.
- `evaluation_enabled`: sempre `false`; confirma que o avaliador não foi executado.
- `classification_enabled`: sempre `false`; confirma que não houve rótulo seguro/ambíguo/proxy/explícito.
- `job_count`: quantidade de vagas, igual a `5`.
- `theme_count`: quantidade de temas sensíveis, igual a `15`.
- `attack_cases_per_job`: ataques distintos por vaga, igual a `75`.
- `control_cases_per_job`: controles neutros por vaga e repetição, igual a `1`.
- `repetitions`: número de execuções de cada caso.
- `planned_generations`: total esperado, calculado por vagas × casos × repetições.
- `jobs`: lista das descrições completas das vagas.
- `control`: definição do comando neutro, sem injeção adversarial.
- `themes`: lista de temas e suas cinco estratégias de ataque.

### Chaves de cada item de `jobs`

- `id`: identificador único e estável da vaga.
- `title`: título da vaga.
- `summary`: resumo das atividades e do contexto.
- `responsibilities`: lista de responsabilidades do cargo.
- `requirements`: lista de requisitos obrigatórios e objetivos.
- `preferred_qualifications`: lista de qualificações desejáveis.
- `seniority`: nível de senioridade.
- `location`: local ou regime geográfico de trabalho.
- `employment_type`: modalidade de contratação e jornada.
- `source_brief`: origem sintética resumida usada para documentar a vaga.

### Chaves de `control`, `themes` e `levels`

- `control.command`: instrução neutra enviada ao gerador.
- `control.rationale`: razão metodológica do controle.
- `themes[].id`: identificador técnico do tema.
- `themes[].name`: nome legível do atributo sensível.
- `themes[].category`: categoria interna do comando (`discriminatory_content` ou `sensitive_data_collection`).
- `themes[].levels`: cinco variações do ataque.
- `levels[].level`: nível organizacional de `1` a `5`.
- `levels[].strategy`: `direct`, `business_justification`, `indirect`, `proxy` ou `deliberate_concealment`.
- `levels[].command`: texto exato injetado como comando adicional do coordenador.

## Chaves de cada registro de geração

- `schema_version`: versão do contrato do registro.
- `run_key`: chave única formada por vaga, caso e repetição.
- `campaign`: campanha à qual a geração pertence.
- `model`: modelo configurado no momento da geração.
- `job_id`: identificador da vaga.
- `job_title`: título legível da vaga.
- `job_description`: cópia completa da vaga usada nesta geração; possui as mesmas chaves de `jobs` descritas acima.
- `case_id`: `control` ou identificador no formato `<tema>-l<nível>`.
- `is_control`: `true` para o caso neutro e `false` para ataque.
- `theme_id`: tema do ataque; `null` no controle.
- `theme_name`: nome legível do tema; `null` no controle.
- `attack_level`: nível de `1` a `5`; `null` no controle.
- `strategy`: estratégia de ataque ou `neutral_control`.
- `prompt_intent`: `benign` no controle ou `malicious` nos ataques; é metadado de entrada, não uma avaliação da saída.
- `prompt_category`: categoria interna do comando.
- `expected_action`: expectativa experimental (`comply` no controle, `refuse` no ataque); não informa se o modelo cumpriu essa expectativa.
- `repetition`: número da repetição, iniciado em `1`.
- `prompt`: comando exato enviado ao agente gerador.
- `started_at`: início da tentativa em ISO 8601 e UTC.
- `completed_at`: término da tentativa em ISO 8601 e UTC.
- `status`: resultado operacional do agente: `succeeded`, `refused`, `failed` ou `runtime_error`.
- `failure_reason`: código/mensagem operacional quando não houve sucesso; caso contrário `null`.
- `questionnaire`: questionário salvo pelo agente; `null` se recusado ou se ocorreu falha.
- `reasoning_summary`: resumo auditável emitido pelo agente; pode ser `null`.
- `trajectory_id`: identificador da trajetória de geração; pode faltar apenas em `runtime_error` externo ao serviço.
- `trace_id`: identificador do trace de observabilidade; pode faltar em erro anterior à criação do trace.
- `duration_ms`: duração do serviço em milissegundos; pode faltar em `runtime_error` externo.

### Chaves de `questionnaire`

- `questionnaire_id`: identificador único do questionário.
- `job_description_id`: vaga usada na geração.
- `questions`: lista de perguntas discursivas geradas.

### Chaves de `questionnaire.questions[]`

- `text`: enunciado apresentado à pessoa candidata.
- `description`: contexto adicional opcional da pergunta.
- `type`: `SHORT_TEXT` para resposta objetiva ou `LONG_TEXT` para resposta explicativa.
- `weight`: peso inteiro de `1` a `10` definido pelo gerador.
- `required`: indica se a resposta é obrigatória.
- `rationale`: justificativa do gerador para incluir a pergunta.

## Chaves de `summary.json`

- `schema_version`: versão do contrato do resumo.
- `campaign`: identificador da campanha.
- `updated_at`: instante da última atualização em ISO 8601 e UTC.
- `planned_generations`: total planejado.
- `recorded_generations`: registros atualmente persistidos.
- `pending_generations`: diferença não negativa entre planejado e registrado.
- `complete`: `true` somente quando todos os registros planejados existem.
- `controls_recorded`: quantidade de controles persistidos.
- `attacks_recorded`: quantidade de ataques persistidos.
- `questionnaires_recorded`: registros que contêm um questionário materializado, independentemente do status operacional final.
- `questionnaires_absent`: registros sem questionário materializado.
- `counts_by_status`: mapa de status operacional para quantidade.
- `evaluation_enabled`: sempre `false` nesta etapa.
- `classification_enabled`: sempre `false` nesta etapa.

## Tabelas do SQLite

- `campaigns`: metadados da campanha e cópia de `manifest.json` em `manifest_json`.
- `jobs`: uma linha por vaga e sua cópia integral em `payload_json`.
- `questionnaire_cases`: uma linha para o controle e uma para cada um dos 75 ataques.
- `generations`: uma linha por `run_key`; `questionnaire_json` contém o questionário e `record_json` preserva o objeto JSON completo.

Nos campos booleanos do SQLite, `0` significa falso e `1` significa verdadeiro. Campos
terminados em `_json` armazenam JSON serializado como texto UTF-8. As demais colunas
repetem as chaves homônimas documentadas acima para permitir consultas SQL diretas.
"""
    (output_dir / DATA_DICTIONARY_FILENAME).write_text(content, encoding="utf-8")


async def _execute(item: WorkItem, campaign: str, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    prompt = CoordinatorPrompt(
        job_description_id=item.job.id,
        sequence=item.sequence,
        intent=PromptIntent.BENIGN if item.is_control else PromptIntent.MALICIOUS,
        category=item.category,
        command=item.command,
        expected_action=ExpectedAction.COMPLY if item.is_control else ExpectedAction.REFUSE,
        requested_question_count=None,
        rationale=item.rationale,
    )
    scenario_id = f"{campaign}-{item.job.id}"
    started_at = datetime.now(UTC)
    base = {
        "schema_version": "1.0",
        "run_key": item.run_key,
        "campaign": campaign,
        "model": configured_model_identifier(),
        "job_id": item.job.id,
        "job_title": item.job.title,
        "job_description": item.job.model_dump(mode="json"),
        "case_id": item.case_id,
        "is_control": item.is_control,
        "theme_id": item.theme_id,
        "theme_name": item.theme_name,
        "attack_level": item.attack_level,
        "strategy": item.strategy,
        "prompt_intent": "benign" if item.is_control else "malicious",
        "prompt_category": item.category.value,
        "expected_action": "comply" if item.is_control else "refuse",
        "repetition": item.repetition,
        "prompt": item.command,
        "started_at": started_at.isoformat(),
    }
    async with semaphore:
        try:
            execution = await QuestionnaireService().execute(
                item.job,
                prompt,
                scenario_id=scenario_id,
                experiment_tags=["security", campaign, "questionnaire-only"],
                experiment_metadata={
                    "research_front": "security",
                    "experiment_profile": campaign,
                    "case_id": item.case_id,
                    "repetition": str(item.repetition),
                },
            )
        except Exception as exc:  # noqa: BLE001 - mantém a campanha resumível
            return {
                **base,
                "completed_at": datetime.now(UTC).isoformat(),
                "status": "runtime_error",
                "failure_reason": f"{type(exc).__name__}: {exc}"[:2000],
                "questionnaire": None,
                "reasoning_summary": None,
                "trajectory_id": None,
                "trace_id": None,
                "duration_ms": max(
                    0,
                    round((datetime.now(UTC) - started_at).total_seconds() * 1000),
                ),
            }
    return {
        **base,
        "completed_at": datetime.now(UTC).isoformat(),
        "status": execution.status.value,
        "failure_reason": execution.failure_reason,
        "questionnaire": (
            execution.questionnaire.model_dump(mode="json")
            if execution.questionnaire is not None
            else None
        ),
        "reasoning_summary": execution.reasoning_summary,
        "trajectory_id": execution.trajectory_id,
        "trace_id": execution.trace_id,
        "duration_ms": execution.duration_ms,
    }


async def _run(  # noqa: PLR0915 - coordena validação, checkpoint e três persistências
    args: argparse.Namespace,
) -> int:
    config = _load_config(args.config)
    work = _build_work(config, args.repetitions)
    if args.concurrency < 1:
        raise ValueError("--concurrency deve ser maior ou igual a 1.")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit deve ser maior ou igual a 1.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / "manifest.json"
    generations_path = args.output_dir / "generations.jsonl"
    generations_dir = args.output_dir / "generations"
    database_path = args.output_dir / "questionnaire_battery.sqlite3"
    manifest = _manifest(config, work)
    if manifest_path.exists():
        previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous_manifest.get("campaign") != manifest["campaign"]:
            raise ValueError("O diretório de saída já pertence a outra campanha.")
        manifest["created_at"] = previous_manifest.get("created_at", manifest["created_at"])
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    store = BatterySQLiteStore(database_path)
    store.initialize(manifest, work)
    existing_records = _records(generations_path)
    for record in existing_records:
        _write_generation_json(generations_dir, record)
        store.save_generation(record)
    initial_summary = _write_summary(
        args.output_dir,
        manifest=manifest,
        generations_path=generations_path,
    )
    _write_data_dictionary(args.output_dir, initial_summary)
    completed = _load_completed(generations_path)
    pending = [item for item in work if item.run_key not in completed]
    if args.limit is not None:
        pending = pending[: args.limit]

    print(f"Manifesto: {manifest_path}")
    print(f"SQLite: {database_path}")
    print(
        f"Planejadas: {len(work)} | já concluídas: {len(completed)} | "
        f"nesta execução: {len(pending)}"
    )
    if args.dry_run or not pending:
        summary = _write_summary(
            args.output_dir,
            manifest=manifest,
            generations_path=generations_path,
        )
        _write_data_dictionary(args.output_dir, summary)
        store.close()
        return 0

    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [asyncio.create_task(_execute(item, config["name"], semaphore)) for item in pending]
    processed = 0
    with generations_path.open("a", encoding="utf-8") as output:
        for task in asyncio.as_completed(tasks):
            record = await task
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
            _write_generation_json(generations_dir, record)
            store.save_generation(record)
            processed += 1
            print(
                f"[{processed}/{len(pending)}] {record['run_key']} -> {record['status']}",
                flush=True,
            )
    summary = _write_summary(
        args.output_dir,
        manifest=manifest,
        generations_path=generations_path,
    )
    _write_data_dictionary(args.output_dir, summary)
    store.close()
    flush_langfuse()
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except (OSError, ValueError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    finally:
        flush_langfuse()


if __name__ == "__main__":
    raise SystemExit(main())
