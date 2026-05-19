"""Registry loading for Agent Router."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CACHE: dict[str, Any] | None = None
_CACHE_DIR: Path | None = None


def _default_routing_dir() -> Path:
    return Path(__file__).resolve().parent / "routing"


def _load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def get_registries(routing_dir: Path | None = None) -> dict[str, Any]:
    """Load and cache all routing registries. Returns dict with lists and status."""
    global _CACHE, _CACHE_DIR
    target = routing_dir or _default_routing_dir()
    if _CACHE is not None and _CACHE_DIR == target:
        return _CACHE

    data: dict[str, Any] = {
        "specialists": [],
        "workflows": [],
        "models": [],
        "task_classes": [],
        "policies": {},
        "_status": {},
    }

    file_map = {
        "specialists": (target / "specialists.json", "specialists"),
        "workflows": (target / "workflows.json", "workflows"),
        "models": (target / "models.copilot.json", "models"),
        "task_classes": (target / "task-classes.json", "classes"),
        "policies": (target / "policies.json", None),
    }

    status: dict[str, bool] = {}
    for key, (path, list_key) in file_map.items():
        try:
            raw = _load_json(path)
            if list_key is not None:
                data[key] = raw.get(list_key, [])
            else:
                data[key] = raw
            status[key] = True
        except Exception:
            status[key] = False

    data["_status"] = status
    _CACHE = data
    _CACHE_DIR = target
    return data


def invalidate_cache() -> None:
    global _CACHE
    _CACHE = None
