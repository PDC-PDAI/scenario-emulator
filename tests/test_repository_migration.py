from __future__ import annotations

import sqlite3

from src.repositories.sqlite import SQLiteRepository


def test_additive_evaluations_migration_preserves_existing_tables(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE scenarios (
            scenario_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE questionnaires (
            questionnaire_id TEXT PRIMARY KEY,
            scenario_id TEXT NOT NULL,
            trajectory_id TEXT NOT NULL
        );
        CREATE TABLE submissions (
            submission_id TEXT PRIMARY KEY,
            questionnaire_id TEXT NOT NULL,
            scenario_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        INSERT INTO scenarios VALUES ('legacy', '2026-01-01T00:00:00Z', '{}');
        """
    )
    connection.commit()
    connection.close()

    repository = SQLiteRepository(path)
    try:
        row = repository._connection.execute(  # noqa: SLF001 - verifica migração real
            "SELECT scenario_id FROM scenarios WHERE scenario_id = 'legacy'"
        ).fetchone()
        table = repository._connection.execute(  # noqa: SLF001
            "SELECT name FROM sqlite_master WHERE type='table' AND name='evaluations'"
        ).fetchone()
        assert row["scenario_id"] == "legacy"
        assert table["name"] == "evaluations"
    finally:
        repository.close()
