from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.schemas.dataset.schema import DatasetCampaignProfile


def load_dataset_campaign_profile(path: Path) -> DatasetCampaignProfile:
    try:
        payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read campaign {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Campaign {path} must contain a YAML object at the root.")
    return DatasetCampaignProfile.model_validate(payload)
