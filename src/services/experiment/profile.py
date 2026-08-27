from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.schemas.experiment.schema import ExperimentProfile


def load_experiment_profile(path: Path) -> ExperimentProfile:
    """Carrega um perfil YAML estrito e devolve caminhos relativos ao cwd."""
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Não foi possível ler o perfil {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML inválido em {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"O perfil {path} deve conter um objeto YAML na raiz.")
    return ExperimentProfile.model_validate(payload)
