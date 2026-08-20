from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from src.clients.langfuse.client import flush_langfuse
from src.repositories.sqlite import SQLiteRepository
from src.schemas.api.schema import (
    HealthResponse,
    JobDescriptionView,
    PublicQuestion,
    QuestionnaireView,
    ScenarioCreateRequest,
    ScenarioSummary,
)
from src.schemas.benchmark.schema import BenchmarkRecord
from src.schemas.evaluation.schema import EvaluationExecution, EvaluationStatus
from src.schemas.questionnaire.schema import QuestionnaireExecution
from src.schemas.scenario.schema import ScenarioRun
from src.schemas.submission.schema import (
    EvaluationInputPayload,
    JobDescriptionContext,
    QuestionnaireSubmission,
    QuestionnaireSubmissionRequest,
    SubmissionStatus,
)
from src.services.evaluation.service import EvaluationService
from src.services.scenario.service import ScenarioService
from src.services.submission.service import SubmissionService
from src.settings import settings

logger = logging.getLogger(__name__)

try:
    _VERSION = version("scenario-emulator")
except PackageNotFoundError:  # pragma: no cover - execução sem instalação do pacote
    _VERSION = "0.1.0"


def _scenario_summary(scenario: ScenarioRun) -> ScenarioSummary:
    return ScenarioSummary(
        scenario_id=scenario.scenario_id,
        created_at=scenario.created_at,
        job_description_id=scenario.job_description.id,
        job_title=scenario.job_description.title,
        execution_count=len(scenario.executions),
        available_questionnaires=sum(
            execution.questionnaire is not None for execution in scenario.executions
        ),
        benchmark_passed=sum(execution.benchmark_passed for execution in scenario.executions),
        evaluation_count=len(scenario.evaluation_executions),
        evaluation_benchmark_passed=sum(
            bool(execution.oracle and execution.oracle.passed)
            for execution in scenario.evaluation_executions
        ),
    )


def _questionnaire_view(
    scenario: ScenarioRun, execution: QuestionnaireExecution
) -> QuestionnaireView:
    questionnaire = execution.questionnaire
    if questionnaire is None:  # pragma: no cover - protegido pelas rotas chamadoras
        raise ValueError("A trajetória não produziu questionário.")
    return QuestionnaireView(
        questionnaire_id=questionnaire.questionnaire_id,
        scenario_id=scenario.scenario_id,
        trajectory_id=execution.trajectory_id,
        job=JobDescriptionView(
            id=scenario.job_description.id,
            title=scenario.job_description.title,
            summary=scenario.job_description.summary,
        ),
        questions=[
            PublicQuestion(
                question_number=number,
                text=question.text,
                description=question.description,
                type=question.type,
                required=question.required,
            )
            for number, question in enumerate(questionnaire.questions, start=1)
        ],
    )


def create_app(  # noqa: PLR0915 - registra explicitamente todos os contratos HTTP
    *,
    repository: SQLiteRepository | None = None,
    scenario_service: ScenarioService | None = None,
    submission_service: SubmissionService | None = None,
    evaluation_service: EvaluationService | None = None,
) -> FastAPI:
    repo = repository or SQLiteRepository(settings.API_DATABASE_PATH)
    scenarios = scenario_service or ScenarioService()
    submissions = submission_service or SubmissionService()
    evaluator = evaluation_service or EvaluationService()

    api = FastAPI(
        title="Scenario Emulator API",
        summary="Geração de cenários e entrega de questionários de RH.",
        description=(
            "Executa a Frente A (vaga → comandos do coordenador → questionários), "
            "gera respostas sintéticas benignas e adversariais, avalia a dimensão de "
            "formulário e mantém o handoff autocontido para integrações externas."
        ),
        version=_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {"name": "health", "description": "Disponibilidade do serviço."},
            {"name": "scenarios", "description": "Execução e consulta da Frente A."},
            {"name": "questionnaires", "description": "Questionários próprios para a UI."},
            {"name": "submissions", "description": "Respostas aguardando avaliação externa."},
            {"name": "evaluations", "description": "Avaliações persistidas de formulário."},
            {"name": "benchmarks", "description": "Artefatos do benchmark defensivo."},
        ],
    )
    if settings.api_cors_origins:
        api.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api_cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    def scenario_or_404(scenario_id: str) -> ScenarioRun:
        scenario = repo.get_scenario(scenario_id)
        if scenario is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Cenário não encontrado."
            )
        return scenario

    def questionnaire_or_404(
        questionnaire_id: str,
    ) -> tuple[ScenarioRun, QuestionnaireExecution]:
        context = repo.get_questionnaire_context(questionnaire_id)
        if context is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Questionário não encontrado ou não gerado.",
            )
        return context

    def submission_or_404(submission_id: str) -> QuestionnaireSubmission:
        submission = repo.get_submission(submission_id)
        if submission is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Resposta de questionário não encontrada.",
            )
        return submission

    @api.get("/health", response_model=HealthResponse, tags=["health"], summary="Health check")
    async def health() -> HealthResponse:
        if not repo.ping():  # pragma: no cover - SQLite lança antes de retornar falso
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return HealthResponse(version=_VERSION)

    @api.post(
        "/api/v1/scenarios",
        response_model=ScenarioRun,
        status_code=status.HTTP_201_CREATED,
        tags=["scenarios"],
        summary="Executa o pipeline completo da Frente A",
    )
    async def create_scenario(payload: ScenarioCreateRequest) -> ScenarioRun:
        try:
            result = await scenarios.run(
                payload.brief,
                benign_count=payload.benign_count,
                malicious_count=payload.malicious_count,
                benign_response_count=payload.benign_response_count,
                malicious_response_count=payload.malicious_response_count,
            )
            repo.save_scenario(result)
            return result
        except Exception as exc:  # noqa: BLE001 - traduz a fronteira do pipeline para HTTP
            logger.exception("Falha ao gerar cenário")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Falha na geração do cenário: {exc}",
            ) from exc
        finally:
            flush_langfuse()

    @api.get(
        "/api/v1/scenarios",
        response_model=list[ScenarioSummary],
        tags=["scenarios"],
        summary="Lista cenários persistidos",
    )
    async def list_scenarios(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[ScenarioSummary]:
        return [_scenario_summary(item) for item in repo.list_scenarios(limit=limit, offset=offset)]

    @api.get(
        "/api/v1/scenarios/{scenario_id}",
        response_model=ScenarioRun,
        tags=["scenarios"],
        summary="Consulta o resultado completo de um cenário",
    )
    async def get_scenario(scenario_id: str) -> ScenarioRun:
        return scenario_or_404(scenario_id)

    @api.get(
        "/api/v1/scenarios/{scenario_id}/questionnaires",
        response_model=list[QuestionnaireView],
        tags=["questionnaires"],
        summary="Lista questionários utilizáveis de um cenário",
    )
    async def list_questionnaires(scenario_id: str) -> list[QuestionnaireView]:
        scenario = scenario_or_404(scenario_id)
        return [
            _questionnaire_view(scenario, execution)
            for execution in scenario.executions
            if execution.questionnaire is not None
        ]

    @api.get(
        "/api/v1/questionnaires/{questionnaire_id}",
        response_model=QuestionnaireView,
        tags=["questionnaires"],
        summary="Obtém um questionário para preenchimento na UI",
    )
    async def get_questionnaire(questionnaire_id: str) -> QuestionnaireView:
        scenario, execution = questionnaire_or_404(questionnaire_id)
        return _questionnaire_view(scenario, execution)

    @api.post(
        "/api/v1/questionnaires/{questionnaire_id}/submissions",
        response_model=QuestionnaireSubmission,
        status_code=status.HTTP_201_CREATED,
        tags=["submissions"],
        summary="Recebe respostas sem avaliá-las",
        description=(
            "Valida numeração e obrigatoriedade, persiste as respostas e as marca como "
            "`ready_for_evaluation`."
        ),
    )
    async def submit_questionnaire(
        questionnaire_id: str,
        payload: QuestionnaireSubmissionRequest,
    ) -> QuestionnaireSubmission:
        scenario, execution = questionnaire_or_404(questionnaire_id)
        questionnaire = execution.questionnaire
        if questionnaire is None:  # pragma: no cover - garantido pelo índice do repositório
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        try:
            submission = submissions.create(
                scenario_id=scenario.scenario_id,
                questionnaire=questionnaire,
                request=payload,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        repo.save_submission(submission)
        return submission

    @api.get(
        "/api/v1/questionnaires/{questionnaire_id}/submissions",
        response_model=list[QuestionnaireSubmission],
        tags=["submissions"],
        summary="Lista respostas prontas para avaliação externa",
    )
    async def list_submissions(
        questionnaire_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[QuestionnaireSubmission]:
        questionnaire_or_404(questionnaire_id)
        return repo.list_submissions(questionnaire_id, limit=limit, offset=offset)

    @api.get(
        "/api/v1/submissions/{submission_id}",
        response_model=QuestionnaireSubmission,
        tags=["submissions"],
        summary="Consulta uma resposta recebida",
    )
    async def get_submission(submission_id: str) -> QuestionnaireSubmission:
        return submission_or_404(submission_id)

    @api.get(
        "/api/v1/submissions/{submission_id}/evaluation-payload",
        response_model=EvaluationInputPayload,
        tags=["submissions"],
        summary="Entrega o pacote para o avaliador externo",
    )
    async def get_evaluation_payload(submission_id: str) -> EvaluationInputPayload:
        submission = submission_or_404(submission_id)
        scenario, execution = questionnaire_or_404(submission.questionnaire_id)
        questionnaire = execution.questionnaire
        if questionnaire is None:  # pragma: no cover - garantido pelo índice do repositório
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        job_context = JobDescriptionContext.model_validate(
            scenario.job_description.model_dump(exclude={"source_brief"})
        )
        return EvaluationInputPayload(
            scenario_id=scenario.scenario_id,
            trajectory_id=execution.trajectory_id,
            job_description=job_context,
            questionnaire=questionnaire,
            submission=submission,
        )

    @api.post(
        "/api/v1/submissions/{submission_id}/evaluate",
        response_model=EvaluationExecution,
        tags=["evaluations"],
        summary="Avalia explicitamente uma submissão",
        description=(
            "Operação idempotente: se a submissão já tiver avaliação persistida, "
            "retorna o resultado existente sem chamar o modelo novamente."
        ),
    )
    async def evaluate_submission(submission_id: str) -> EvaluationExecution:
        submission = submission_or_404(submission_id)
        existing = repo.get_evaluation_for_submission(submission_id)
        if existing is not None:
            return existing
        scenario, questionnaire_execution = questionnaire_or_404(submission.questionnaire_id)
        questionnaire = questionnaire_execution.questionnaire
        if questionnaire is None:  # pragma: no cover - garantido pelo repositório
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        node_id = (
            f"{scenario.scenario_id}.questionnaire."
            f"{questionnaire_execution.coordinator_prompt.sequence:03d}"
        )
        try:
            evaluation = await evaluator.evaluate(
                scenario_id=scenario.scenario_id,
                job=scenario.job_description,
                questionnaire=questionnaire,
                submission=submission,
                response_case=None,
                depends_on=[node_id],
            )
            submission.status = (
                SubmissionStatus.EVALUATED
                if evaluation.status is EvaluationStatus.SUCCEEDED
                else SubmissionStatus.EVALUATION_FAILED
            )
            evaluation.submission = submission
            repo.save_submission(submission)
            repo.save_evaluation(evaluation)
            return evaluation
        finally:
            flush_langfuse()

    @api.get(
        "/api/v1/submissions/{submission_id}/evaluation",
        response_model=EvaluationExecution,
        tags=["evaluations"],
        summary="Consulta a avaliação de uma submissão",
    )
    async def get_submission_evaluation(submission_id: str) -> EvaluationExecution:
        submission_or_404(submission_id)
        evaluation = repo.get_evaluation_for_submission(submission_id)
        if evaluation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Avaliação da submissão ainda não existe.",
            )
        return evaluation

    @api.get(
        "/api/v1/scenarios/{scenario_id}/evaluations",
        response_model=list[EvaluationExecution],
        tags=["evaluations"],
        summary="Lista avaliações persistidas do cenário",
    )
    async def list_evaluations(
        scenario_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[EvaluationExecution]:
        scenario_or_404(scenario_id)
        return repo.list_evaluations(scenario_id, limit=limit, offset=offset)

    @api.get(
        "/api/v1/scenarios/{scenario_id}/benchmark-records",
        response_model=list[BenchmarkRecord],
        tags=["benchmarks"],
        summary="Obtém os registros estruturados do benchmark",
    )
    async def get_benchmark_records(scenario_id: str) -> list[BenchmarkRecord]:
        return scenario_or_404(scenario_id).benchmark_records

    @api.get(
        "/api/v1/scenarios/{scenario_id}/benchmark.jsonl",
        response_class=Response,
        tags=["benchmarks"],
        summary="Exporta o benchmark em JSONL",
        responses={200: {"content": {"application/x-ndjson": {}}}},
    )
    async def get_benchmark_jsonl(scenario_id: str) -> Response:
        records = scenario_or_404(scenario_id).benchmark_records
        body = "\n".join(record.model_dump_json(by_alias=True) for record in records) + "\n"
        return Response(content=body, media_type="application/x-ndjson")

    return api


app = create_app()
