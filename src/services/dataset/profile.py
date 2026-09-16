from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.schemas.dataset.schema import DatasetCampaignProfile


def load_dataset_campaign_profile(path: Path) -> DatasetCampaignProfile:
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Não foi possível ler a campanha {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML inválido em {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"A campanha {path} deve conter um objeto YAML na raiz.")
    return DatasetCampaignProfile.model_validate(payload)
