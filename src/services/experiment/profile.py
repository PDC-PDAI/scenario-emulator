from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.schemas.experiment.schema import ExperimentProfile


def load_experiment_profile(path: Path) -> ExperimentProfile:
    """Load a strict YAML profile and return paths relative to the cwd."""
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read profile {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Profile {path} must contain a YAML object at the root.")
    return ExperimentProfile.model_validate(payload)
