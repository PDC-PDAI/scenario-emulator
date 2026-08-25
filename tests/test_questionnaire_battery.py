from __future__ import annotations

from pathlib import Path

from scripts.run_questionnaire_battery import _build_work, _load_config

CONFIG_PATH = Path("configs/questionnaire_battery.yaml")
JOB_COUNT = 5
THEME_COUNT = 15
ATTACKS_PER_JOB = THEME_COUNT * 5
CASES_PER_JOB = ATTACKS_PER_JOB + 1
TOTAL_CASES = JOB_COUNT * CASES_PER_JOB


def test_questionnaire_battery_has_expected_matrix_and_controls() -> None:
    config = _load_config(CONFIG_PATH)
    work = _build_work(config, repetitions=1)

    assert len(work) == TOTAL_CASES
    assert len({item.job.id for item in work}) == JOB_COUNT
    assert sum(item.is_control for item in work) == JOB_COUNT
    assert sum(not item.is_control for item in work) == JOB_COUNT * ATTACKS_PER_JOB
    assert {item.attack_level for item in work if not item.is_control} == {1, 2, 3, 4, 5}
    assert len({item.run_key for item in work}) == TOTAL_CASES

    for job_id in {item.job.id for item in work}:
        job_work = [item for item in work if item.job.id == job_id]
        assert len(job_work) == CASES_PER_JOB
        assert sum(item.is_control for item in job_work) == 1
        assert len({item.theme_id for item in job_work if not item.is_control}) == THEME_COUNT


def test_questionnaire_battery_repetitions_apply_to_controls_and_attacks() -> None:
    config = _load_config(CONFIG_PATH)
    work = _build_work(config, repetitions=2)

    assert len(work) == TOTAL_CASES * 2
    assert {item.repetition for item in work} == {1, 2}
    assert len({item.run_key for item in work}) == TOTAL_CASES * 2
