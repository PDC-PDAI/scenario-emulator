from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from src.schemas.evaluation.schema import EvaluationExecution
from src.schemas.questionnaire.schema import QuestionnaireExecution
from src.schemas.scenario.schema import ScenarioRun
from src.schemas.submission.schema import QuestionnaireSubmission


class SQLiteRepository:
    """Persistência local simples para o ciclo HTTP do emulador.

    O cenário continua sendo o agregado canônico. A tabela de questionários mantém
    apenas o índice necessário para localizar rapidamente o cenário e a trajetória.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = (
            ":memory:"
            if str(database_path) == ":memory:"
            else str(Path(database_path).expanduser())
        )
        if self.database_path != ":memory:":
            Path(self.database_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if self.database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scenarios (
                    scenario_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS questionnaires (
                    questionnaire_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    trajectory_id TEXT NOT NULL,
                    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT PRIMARY KEY,
                    questionnaire_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (questionnaire_id)
                        REFERENCES questionnaires(questionnaire_id) ON DELETE CASCADE,
                    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    submission_id TEXT NOT NULL UNIQUE,
                    questionnaire_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (submission_id)
                        REFERENCES submissions(submission_id) ON DELETE CASCADE,
                    FOREIGN KEY (questionnaire_id)
                        REFERENCES questionnaires(questionnaire_id) ON DELETE CASCADE,
                    FOREIGN KEY (scenario_id) REFERENCES scenarios(scenario_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_questionnaires_scenario
                    ON questionnaires(scenario_id);
                CREATE INDEX IF NOT EXISTS idx_submissions_questionnaire
                    ON submissions(questionnaire_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_evaluations_scenario
                    ON evaluations(scenario_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_evaluations_questionnaire
                    ON evaluations(questionnaire_id, created_at DESC);
                """
            )

    def save_scenario(self, scenario: ScenarioRun) -> None:
        questionnaire_rows = [
            (
                execution.questionnaire.questionnaire_id,
                scenario.scenario_id,
                execution.trajectory_id,
            )
            for execution in scenario.executions
            if execution.questionnaire is not None
        ]
        evaluations = scenario.evaluation_executions
        submission_rows = [
            (
                evaluation.submission.submission_id,
                evaluation.submission.questionnaire_id,
                evaluation.submission.scenario_id,
                evaluation.submission.submitted_at.isoformat(),
                evaluation.submission.model_dump_json(),
            )
            for evaluation in evaluations
        ]
        evaluation_rows = [
            (
                evaluation.evaluation_id,
                evaluation.submission.submission_id,
                evaluation.questionnaire_id,
                evaluation.scenario_id,
                evaluation.created_at.isoformat(),
                evaluation.model_dump_json(),
            )
            for evaluation in evaluations
        ]
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO scenarios (scenario_id, created_at, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(scenario_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    payload_json = excluded.payload_json
                """,
                (
                    scenario.scenario_id,
                    scenario.created_at.isoformat(),
                    scenario.model_dump_json(by_alias=True),
                ),
            )
            self._connection.executemany(
                """
                INSERT INTO questionnaires (questionnaire_id, scenario_id, trajectory_id)
                VALUES (?, ?, ?)
                ON CONFLICT(questionnaire_id) DO UPDATE SET
                    scenario_id = excluded.scenario_id,
                    trajectory_id = excluded.trajectory_id
                """,
                questionnaire_rows,
            )
            self._connection.executemany(
                """
                INSERT INTO submissions (
                    submission_id, questionnaire_id, scenario_id, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(submission_id) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                submission_rows,
            )
            self._connection.executemany(
                """
                INSERT INTO evaluations (
                    evaluation_id, submission_id, questionnaire_id,
                    scenario_id, created_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(submission_id) DO UPDATE SET
                    evaluation_id = excluded.evaluation_id,
                    created_at = excluded.created_at,
                    payload_json = excluded.payload_json
                """,
                evaluation_rows,
            )

    def get_scenario(self, scenario_id: str) -> ScenarioRun | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM scenarios WHERE scenario_id = ?",
                (scenario_id,),
            ).fetchone()
        return ScenarioRun.model_validate_json(row["payload_json"]) if row else None

    def list_scenarios(self, *, limit: int = 50, offset: int = 0) -> list[ScenarioRun]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM scenarios
                ORDER BY created_at DESC, scenario_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [ScenarioRun.model_validate_json(row["payload_json"]) for row in rows]

    def get_questionnaire_context(
        self, questionnaire_id: str
    ) -> tuple[ScenarioRun, QuestionnaireExecution] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT scenario_id, trajectory_id
                FROM questionnaires
                WHERE questionnaire_id = ?
                """,
                (questionnaire_id,),
            ).fetchone()
        if row is None:
            return None
        scenario = self.get_scenario(row["scenario_id"])
        if scenario is None:
            return None
        execution = next(
            (
                item
                for item in scenario.executions
                if item.trajectory_id == row["trajectory_id"]
                and item.questionnaire is not None
                and item.questionnaire.questionnaire_id == questionnaire_id
            ),
            None,
        )
        return (scenario, execution) if execution is not None else None

    def save_submission(self, submission: QuestionnaireSubmission) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO submissions (
                    submission_id,
                    questionnaire_id,
                    scenario_id,
                    created_at,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(submission_id) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (
                    submission.submission_id,
                    submission.questionnaire_id,
                    submission.scenario_id,
                    submission.submitted_at.isoformat(),
                    submission.model_dump_json(),
                ),
            )

    def save_evaluation(self, evaluation: EvaluationExecution) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO evaluations (
                    evaluation_id,
                    submission_id,
                    questionnaire_id,
                    scenario_id,
                    created_at,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(submission_id) DO UPDATE SET
                    evaluation_id = excluded.evaluation_id,
                    created_at = excluded.created_at,
                    payload_json = excluded.payload_json
                """,
                (
                    evaluation.evaluation_id,
                    evaluation.submission.submission_id,
                    evaluation.questionnaire_id,
                    evaluation.scenario_id,
                    evaluation.created_at.isoformat(),
                    evaluation.model_dump_json(),
                ),
            )

    def get_submission(self, submission_id: str) -> QuestionnaireSubmission | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return QuestionnaireSubmission.model_validate_json(row["payload_json"]) if row else None

    def list_submissions(
        self,
        questionnaire_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[QuestionnaireSubmission]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM submissions
                WHERE questionnaire_id = ?
                ORDER BY created_at DESC, submission_id DESC
                LIMIT ? OFFSET ?
                """,
                (questionnaire_id, limit, offset),
            ).fetchall()
        return [QuestionnaireSubmission.model_validate_json(row["payload_json"]) for row in rows]

    def get_evaluation(self, evaluation_id: str) -> EvaluationExecution | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM evaluations WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
        return EvaluationExecution.model_validate_json(row["payload_json"]) if row else None

    def get_evaluation_for_submission(self, submission_id: str) -> EvaluationExecution | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload_json FROM evaluations WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
        return EvaluationExecution.model_validate_json(row["payload_json"]) if row else None

    def list_evaluations(
        self,
        scenario_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EvaluationExecution]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM evaluations
                WHERE scenario_id = ?
                ORDER BY created_at DESC, evaluation_id DESC
                LIMIT ? OFFSET ?
                """,
                (scenario_id, limit, offset),
            ).fetchall()
        return [EvaluationExecution.model_validate_json(row["payload_json"]) for row in rows]

    def ping(self) -> bool:
        with self._lock:
            row = self._connection.execute("SELECT 1 AS healthy").fetchone()
        return bool(row and row["healthy"] == 1)

    def close(self) -> None:
        with self._lock:
            self._connection.close()
