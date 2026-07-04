"""模型推理封装: 加载 ml/models 下的 LGB 模型 + 元数据, 对特征行打分。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MODELS_DIR = Path(__file__).resolve().parent / "models"


class MLScorer:
    def __init__(self, models_dir: str | Path | None = None):
        self.dir = Path(models_dir) if models_dir else MODELS_DIR
        self.meta: dict[str, Any] | None = None
        self.lifecycle_meta: dict[str, Any] | None = None
        self.cols: list[str] = []
        self._cols: dict[str, list[str]] = {}
        self._boosters: dict[str, Any] = {}
        self._lifecycle_boosters: dict[str, Any] = {}
        self._np = None
        self.error = ""
        self.load()

    def load(self) -> None:
        try:
            import numpy as np
            import lightgbm as lgb
        except Exception as exc:  # 缺依赖 -> 优雅降级
            self.error = f"missing deps: {exc}"
            return
        self._np = np
        meta_p = self.dir / "meta.json"
        if not meta_p.exists():
            self.error = "no meta.json"
            return
        try:
            self.meta = json.loads(meta_p.read_text(encoding="utf-8"))
            self.cols = list(self.meta["feature_cols"])
            long_cols = list(self.meta.get("long_feature_cols", self.cols))
            self._cols = {"dump": self.cols, "top": self.cols, "long": long_cols}
            for task in ("dump", "top", "long"):
                p = self.dir / f"{task}.txt"
                if p.exists():
                    self._boosters[task] = lgb.Booster(model_file=str(p))
            lifecycle_dir = self.dir / "lifecycle"
            lifecycle_meta = lifecycle_dir / "meta.json"
            if lifecycle_meta.exists():
                self.lifecycle_meta = json.loads(lifecycle_meta.read_text(encoding="utf-8"))
                for name in self.lifecycle_meta.get("models", {}):
                    p = lifecycle_dir / f"{name}.txt"
                    if p.exists():
                        self._lifecycle_boosters[name] = lgb.Booster(model_file=str(p))
        except Exception as exc:
            self.error = f"load failed: {exc}"
            self.meta = None
            self._boosters = {}
            self.lifecycle_meta = None
            self._lifecycle_boosters = {}

    @property
    def ready(self) -> bool:
        return bool(self._boosters) and self.meta is not None

    @property
    def lifecycle_ready(self) -> bool:
        required = {"long_pump_event", "long_start_quality", "fast_top", "fast_short", "slow_warning", "slow_short"}
        return self.lifecycle_meta is not None and required.issubset(self._lifecycle_boosters)

    @property
    def lifecycle_router_ready(self) -> bool:
        return self.lifecycle_meta is not None and "family_router" in self._lifecycle_boosters

    def score(self, feat_row: Any, task: str) -> float | None:
        booster = self._boosters.get(task)
        if booster is None or self._np is None:
            return None
        cols = self._cols.get(task, self.cols)
        get = feat_row.get if hasattr(feat_row, "get") else (lambda k, d=None: feat_row[k])
        x = self._np.array([[float(get(c, self._np.nan)) for c in cols]], dtype="float64")
        return float(booster.predict(x)[0])

    def threshold(self, task: str) -> float | None:
        return (self.meta or {}).get(task, {}).get("thr")

    def threshold_high(self, task: str) -> float | None:
        return (self.meta or {}).get(task, {}).get("thr_high")

    def lifecycle_score(self, feat_row: Any, model_name: str) -> float | None:
        booster = self._lifecycle_boosters.get(model_name)
        if booster is None or self._np is None or self.lifecycle_meta is None:
            return None
        cols = self.lifecycle_columns(model_name)
        get = feat_row.get if hasattr(feat_row, "get") else (lambda k, d=None: feat_row[k])
        x = self._np.array([[float(get(c, self._np.nan)) for c in cols]], dtype="float64")
        return float(booster.predict(x)[0])

    def lifecycle_probabilities(self, feat_row: Any, model_name: str) -> dict[str, float] | None:
        booster = self._lifecycle_boosters.get(model_name)
        if booster is None or self._np is None or self.lifecycle_meta is None:
            return None
        model = self.lifecycle_meta.get("models", {}).get(model_name, {})
        classes = list(model.get("classes") or [])
        if not classes:
            return None
        cols = self.lifecycle_columns(model_name)
        get = feat_row.get if hasattr(feat_row, "get") else (lambda k, d=None: feat_row[k])
        x = self._np.array([[float(get(c, self._np.nan)) for c in cols]], dtype="float64")
        pred = booster.predict(x)
        arr = self._np.asarray(pred, dtype="float64").reshape(-1)
        if len(arr) != len(classes):
            return None
        return {str(cls): float(arr[i]) for i, cls in enumerate(classes)}

    def lifecycle_columns(self, model_name: str) -> list[str]:
        if self.lifecycle_meta is None:
            return []
        model = self.lifecycle_meta.get("models", {}).get(model_name, {})
        feature_set = model.get("feature_set", "")
        cols = self.lifecycle_meta.get("feature_sets", {}).get(feature_set)
        if cols:
            return list(cols)
        try:
            from . import lifecycle as lifecycle_features

            defaults = {
                "long": lifecycle_features.LONG_FEATURES,
                "fast": lifecycle_features.FAST_FEATURES,
                "slow": lifecycle_features.SLOW_FEATURES,
                "router": lifecycle_features.ROUTER_FEATURES,
            }
            return list(defaults.get(feature_set, []))
        except Exception:
            return []

    def lifecycle_threshold(self, model_name: str) -> float | None:
        if self.lifecycle_meta is None:
            return None
        value = self.lifecycle_meta.get("models", {}).get(model_name, {}).get("threshold")
        return float(value) if value is not None else None

    def lifecycle_threshold_high(self, model_name: str) -> float | None:
        if self.lifecycle_meta is None:
            return None
        value = self.lifecycle_meta.get("models", {}).get(model_name, {}).get("threshold_high")
        return float(value) if value is not None else None

    def lifecycle_long_score(self, feat_row: Any) -> dict[str, float | None]:
        pump = self.lifecycle_score(feat_row, "long_pump_event")
        quality = self.lifecycle_score(feat_row, "long_start_quality")
        weights = (self.lifecycle_meta or {}).get("long_score", {}).get("weights", {"pump": 0.65, "quality": 0.35})
        if pump is None or quality is None:
            score = None
        else:
            score = float(weights.get("pump", 0.65)) * pump + float(weights.get("quality", 0.35)) * quality
        return {"pump": pump, "quality": quality, "score": score}

    def lifecycle_long_threshold(self, high: bool = False) -> float | None:
        cfg = (self.lifecycle_meta or {}).get("long_score", {})
        key = "threshold_high" if high else "threshold"
        value = cfg.get(key)
        return float(value) if value is not None else None

    def info(self) -> dict[str, Any]:
        info = dict(self.meta or {"ready": False, "error": self.error})
        if self.lifecycle_meta:
            lite = dict(self.lifecycle_meta)
            lite.pop("feature_sets", None)
            info["lifecycle"] = lite
            info["lifecycle_ready"] = self.lifecycle_ready
            info["lifecycle_router_ready"] = self.lifecycle_router_ready
        return info
