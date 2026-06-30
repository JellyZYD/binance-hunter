from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.json"


def resolve_path(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache(maxsize=8)
def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else DEFAULT_SETTINGS
    if not cfg_path.is_absolute():
        cfg_path = PROJECT_ROOT / cfg_path
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    apply_env_overrides(data)
    for key in ("db_path", "alerts_dir", "cache_dir", "reports_dir"):
        data["paths"][key] = str(resolve_path(data["paths"][key]))
    return data


def apply_env_overrides(data: dict[str, Any]) -> None:
    proxy = os.getenv("HUNTER_NETWORK_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if proxy:
        data.setdefault("network", {})["proxy"] = proxy

    ws_base_url = os.getenv("HUNTER_WS_BASE_URL")
    if ws_base_url:
        data.setdefault("network", {})["ws_base_url"] = ws_base_url

    db_path = os.getenv("HUNTER_DB_PATH")
    if db_path:
        data.setdefault("paths", {})["db_path"] = db_path

    alerts_dir = os.getenv("HUNTER_ALERTS_DIR")
    if alerts_dir:
        data.setdefault("paths", {})["alerts_dir"] = alerts_dir

    wecom_url = os.getenv("WECOM_WEBHOOK_URL")
    if wecom_url:
        data.setdefault("notify", {})["wecom_webhook_url"] = wecom_url


def ensure_dirs(settings: dict[str, Any]) -> dict[str, Path]:
    paths = {
        "db": Path(settings["paths"]["db_path"]),
        "alerts": Path(settings["paths"]["alerts_dir"]),
        "cache": Path(settings["paths"]["cache_dir"]),
        "reports": Path(settings["paths"]["reports_dir"]),
    }
    for key, path in paths.items():
        if key == "db":
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return paths
