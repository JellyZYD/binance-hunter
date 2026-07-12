"""Search family-specific exit profile assignments for waterfall combos."""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backtest_waterfall_combo import CONFIGS, metrics, period_metrics, replay_combo  # noqa: E402


DEFAULT_FAMILIES = ("post_pump", "downtrend_continuation", "momentum_dump", "other", "range_breakdown")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = pd.read_csv(args.candidates)
    if candidates.empty:
        raise SystemExit("empty candidates")
    candidates["entry_dt"] = pd.to_datetime(candidates["entry_time"], unit="ms")
    configs = [cfg for cfg in CONFIGS if cfg.name in set(args.configs.split(","))]
    families = tuple(x.strip() for x in args.families.split(",") if x.strip())
    profile_choices = choose_profiles(candidates, families, args.top_profiles_per_family, args.validation_start, args.min_family_validation_trades)
    combos = list(itertools.product(*(profile_choices[fam] for fam in families)))
    print(json.dumps({"family_choices": profile_choices, "assignments": len(combos)}, ensure_ascii=False), flush=True)

    rows: list[dict[str, Any]] = []
    details: dict[str, pd.DataFrame] = {}
    for combo_idx, values in enumerate(combos, 1):
        assignment = dict(zip(families, values))
        mixed = materialize_assignment(candidates, assignment)
        if mixed.empty:
            continue
        assignment_name = assignment_label(assignment)
        for config in configs:
            selected = replay_combo(mixed, config)
            row = {
                "assignment": assignment_name,
                "config": config.name,
                **metrics(selected),
                **{f"profile_{fam}": assignment[fam] for fam in families},
            }
            rows.append(row)
            for p_row in period_metrics(selected, config.name):
                p_row["assignment"] = assignment_name
                p_row.update({f"profile_{fam}": assignment[fam] for fam in families})
                rows.append(p_row)
            key = f"{assignment_name}__{config.name}"
            details[key] = selected
        if args.progress_every and combo_idx % args.progress_every == 0:
            print(f"searched {combo_idx}/{len(combos)}", flush=True)

    summary = pd.DataFrame(rows)
    if not summary.empty:
        summary = add_scores(summary)
        summary = summary.sort_values(["period", "score", "profit_factor", "avg_ret"], ascending=[True, False, False, False])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"waterfall_family_exit_mix_summary_{stamp}.csv"
    report_path = out_dir / f"waterfall_family_exit_mix_report_{stamp}.md"
    summary.to_csv(csv_path, index=False)
    report_path.write_text(render_report(summary, args.min_per_day), encoding="utf-8")
    if not summary.empty:
        top_recent = summary[(summary["period"] == "recent90") & (summary["per_day"] >= args.min_per_day)].sort_values("score", ascending=False).head(10)
        for _, row in top_recent.iterrows():
            key = f"{row['assignment']}__{row['config']}"
            df = details.get(key)
            if df is not None and not df.empty:
                safe = str(key).replace("|", "_").replace(":", "-")
                df.to_csv(out_dir / f"waterfall_family_exit_mix_{safe}_{stamp}.csv", index=False)
    print(json.dumps({"summary": str(csv_path), "report": str(report_path)}, ensure_ascii=False), flush=True)
    return 0


def choose_profiles(candidates: pd.DataFrame, families: tuple[str, ...], top_k: int, validation_start: str, min_val: int) -> dict[str, list[str]]:
    val_start = pd.Timestamp(validation_start)
    out: dict[str, list[str]] = {}
    for family in families:
        base = candidates[candidates["family"] == family].copy()
        if base.empty:
            out[family] = ["original"]
            continue
        rows = []
        for profile, g in base.groupby("exit_profile"):
            val = g[g["entry_dt"] >= val_start]
            train = g[g["entry_dt"] < val_start]
            if len(val) < min_val or len(train) < min_val:
                continue
            val_m = local_metrics(val)
            train_m = local_metrics(train)
            score = (
                math.log1p(max(0.0, val_m["pf"])) * 4.0
                + max(0.0, val_m["avg_ret"]) * 35.0
                + max(0.0, train_m["pf"]) * 0.15
                + val_m["big3"] * 1.5
                - val_m["stop"] * 0.6
                - max(0.0, val_m["p80_mae"] - 0.04) * 8.0
            )
            rows.append({
                "profile": str(profile),
                "score": score,
                "val_trades": len(val),
                "val_pf": val_m["pf"],
                "val_avg": val_m["avg_ret"],
                "val_p80_mae": val_m["p80_mae"],
                "train_pf": train_m["pf"],
            })
        choices = [row["profile"] for row in sorted(rows, key=lambda x: x["score"], reverse=True)[:top_k]]
        if "original" not in choices and "original" in set(base["exit_profile"]):
            choices.append("original")
        out[family] = choices or ["original"]
    return out


def local_metrics(df: pd.DataFrame) -> dict[str, float]:
    pos = float(df.loc[df["ret"] > 0, "ret"].sum())
    neg = float(-df.loc[df["ret"] < 0, "ret"].sum())
    return {
        "pf": pos / neg if neg > 0 else 99.0,
        "avg_ret": float(df["ret"].mean()),
        "big3": float((df["ret"] >= 0.03).mean()),
        "stop": float((df["exit_reason"] == "stop").mean()),
        "p80_mae": float(df["mae"].quantile(0.8)),
    }


def materialize_assignment(candidates: pd.DataFrame, assignment: dict[str, str]) -> pd.DataFrame:
    masks = []
    for family, profile in assignment.items():
        masks.append((candidates["family"] == family) & (candidates["exit_profile"] == profile))
    out = candidates[np.logical_or.reduce(masks)].copy()
    return out.drop(columns=["entry_dt"], errors="ignore").sort_values(["entry_time", "symbol", "rule"]).reset_index(drop=True)


def assignment_label(assignment: dict[str, str]) -> str:
    short = {
        "post_pump": "post",
        "downtrend_continuation": "down",
        "momentum_dump": "mom",
        "other": "oth",
        "range_breakdown": "rng",
    }
    return "|".join(f"{short.get(fam, fam)}:{assignment[fam]}" for fam in assignment)


def add_scores(summary: pd.DataFrame) -> pd.DataFrame:
    out = summary.copy()
    out["score"] = (
        np.log1p(out["profit_factor"].fillna(0).clip(lower=0)) * 4.0
        + out["avg_ret"].fillna(0).clip(lower=0) * 0.35
        + np.log1p(out["per_day"].fillna(0)) * 1.0
        + out["big3_rate"].fillna(0) * 0.015
        - out["stop_rate"].fillna(0) * 0.01
        - (out["p80_mae"].fillna(0) - 4.0).clip(lower=0) * 0.2
    )
    return out


def render_report(summary: pd.DataFrame, min_per_day: float) -> str:
    lines = ["# Waterfall Family Exit Mix Search", ""]
    if summary.empty:
        return "# Waterfall Family Exit Mix Search\n\nNo results.\n"
    for period in ("holdout_202604+", "recent90", "all"):
        part = summary[(summary["period"] == period) & (summary["per_day"] >= min_per_day)].copy()
        if part.empty:
            continue
        part = part.sort_values(["score", "profit_factor", "avg_ret"], ascending=False).head(30)
        lines.append(f"## {period}")
        cols = [
            "assignment", "config", "trades", "per_day", "win_rate", "avg_ret", "median_ret",
            "profit_factor", "median_mae", "p80_mae", "median_mfe", "p80_mfe",
            "big3_rate", "big5_rate", "stop_rate", "median_hold_min",
        ]
        lines.append(part[[c for c in cols if c in part.columns]].to_markdown(index=False))
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--out-dir", default="backend/storage/ml/waterfall_family_exit_mix")
    parser.add_argument("--configs", default="combo_recent_core_4h,combo_recent_quality_2h,combo_recent_high_pf_2h")
    parser.add_argument("--families", default=",".join(DEFAULT_FAMILIES))
    parser.add_argument("--top-profiles-per-family", type=int, default=5)
    parser.add_argument("--validation-start", default="2026-04-01")
    parser.add_argument("--min-family-validation-trades", type=int, default=20)
    parser.add_argument("--min-per-day", type=float, default=2.0)
    parser.add_argument("--progress-every", type=int, default=100)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
