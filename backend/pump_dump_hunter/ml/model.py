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
        self.cols: list[str] = []
        self._cols: dict[str, list[str]] = {}
        self._boosters: dict[str, Any] = {}
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
        except Exception as exc:
            self.error = f"load failed: {exc}"
            self.meta = None
            self._boosters = {}

    @property
    def ready(self) -> bool:
        return bool(self._boosters) and self.meta is not None

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

    def info(self) -> dict[str, Any]:
        return self.meta or {"ready": False, "error": self.error}
