"""本地训练管线: 读 parquet -> 候选/事件级标签 -> 训 见顶/下跌启动 两个 LGB -> 存模型+元数据。

只在本地跑(服务器 2C2G 不训练)。训练后把 ml/models/ 提交推送, 服务器 update.sh 拉取。

用法:
    python -m pump_dump_hunter.ml.train --source "E:\\2C2G\\币安数据库" --days 365
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import lightgbm as lgb
from sklearn.metrics import roc_auc_score

from .features import (
    compute_features, top_setup_flags, dump_setup_flags, feature_columns, LOOKBACK,
    compute_flow_features, flow_columns, long_feature_columns,
    LONG_RET2_MIN, LONG_VOLR30_MIN, LONG_HEAT_24H, LONG_HEAT_4H, LONG_HEAT_12H,
    LONG_CPOS_MIN, LONG_UWICK_MAX, LONG_DIST_EMA21_MAX, PUMP_4H, PUMP_12H, PUMP_1D,
)

MODELS_DIR = Path(__file__).resolve().parent / "models"
EXCLUDE = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT",
    "XAUUSDT", "XAGUSDT", "XAUTUSDT", "PAXGUSDT", "CLUSDT", "NATGASUSDT", "QQQUSDT", "SPXUSDT",
    "SPYUSDT", "AAPLUSDT", "AMZNUSDT", "AMDUSDT", "COINUSDT", "CRCLUSDT", "EWYUSDT", "GOOGUSDT",
    "INTCUSDT", "METAUSDT", "MSTRUSDT", "MSFTUSDT", "MUUSDT", "NFLXUSDT", "NVDAUSDT", "SNDKUSDT", "TSLAUSDT",
}
DAY = 86_400_000
FEATS = feature_columns()


def agg15(path, start, end):
    t = pq.read_table(path, columns=["timestamp", "open", "high", "low", "close", "quote_volume", "taker_buy_quote_volume"],
                      filters=[("timestamp", ">=", start), ("timestamp", "<=", end)]).to_pandas()
    if t.empty:
        return None
    t = t.drop_duplicates("timestamp").sort_values("timestamp")
    t["b"] = (t.timestamp // 900000) * 900000
    g = t.groupby("b").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                           close=("close", "last"), qv=("quote_volume", "sum"), tbq=("taker_buy_quote_volume", "sum"),
                           cnt=("close", "size"))
    return g[g.cnt >= 10].reset_index()


def fwd(s, H, kind):
    rev = s.iloc[::-1]
    r = rev.rolling(H, min_periods=1).min() if kind == "min" else rev.rolling(H, min_periods=1).max()
    return r.iloc[::-1].shift(-1)


def build_rows(source, days):
    end = 0
    files = [f for f in glob.glob(os.path.join(source, "klines", "*.parquet"))
             if os.path.basename(f)[:-8].upper() not in EXCLUDE]
    # 数据结束时间取所有文件的最大 timestamp 近似:用一个已知近端读法
    for f in files[:5]:
        try:
            pf = pq.ParquetFile(f)
            for i in range(pf.metadata.num_row_groups):
                st = pf.metadata.row_group(i).column(0).statistics
                if st and st.has_min_max:
                    end = max(end, int(st.max))
        except Exception:
            pass
    if not end:
        end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = end - days * DAY
    rows = []
    for path in files:
        sym = os.path.basename(path)[:-8].upper()
        try:
            g = agg15(path, start - 3 * DAY, end)
        except Exception:
            continue
        if g is None or len(g) < LOOKBACK + 300:
            continue
        F = compute_features(g)
        c, hi, lo = g.close.values, g.high.values, g.low.values
        n = len(g)
        fut72d = c / fwd(pd.Series(lo), 288, "min").values - 1
        fut72u = fwd(pd.Series(hi), 288, "max").values / c - 1
        fut24u = fwd(pd.Series(hi), 96, "max").values / c - 1
        top_s = top_setup_flags(F).values
        dump_s = dump_setup_flags(F).values
        dd96 = F["dd_96"].values
        valid = np.zeros(n, bool)
        valid[LOOKBACK:max(LOOKBACK, n - 289)] = True
        finite = np.isfinite(fut72d) & F[FEATS].notna().all(axis=1).values
        cand = (top_s | dump_s) & valid & finite
        idx = np.where(cand)[0]
        if len(idx) == 0:
            continue
        reached5 = np.zeros(n, bool); adv_before = np.full(n, np.nan)
        for i in idx:
            if not dump_s[i]:
                continue
            tgt = c[i] * 0.95; seg_hi = hi[i + 1:i + 49]; seg_lo = lo[i + 1:i + 49]
            hit = np.where(seg_lo <= tgt)[0]
            if len(hit):
                j = hit[0]; reached5[i] = True; adv_before[i] = seg_hi[:j + 1].max() / c[i] - 1
        top_good = top_s & (dd96 >= -0.08) & (fut72d >= 0.15) & (fut24u <= 0.10)
        dump_good = dump_s & reached5 & (np.nan_to_num(adv_before, nan=1.0) <= 0.08) & (fut72d >= 0.10)
        sub = F.iloc[idx][FEATS].copy()
        sub["top_setup"] = top_s[idx].astype("int8"); sub["dump_setup"] = dump_s[idx].astype("int8")
        sub["top_good"] = top_good[idx].astype("int8"); sub["dump_good"] = dump_good[idx].astype("int8")
        sub["ts"] = g["b"].values[idx]; sub["symbol"] = sym
        ev = np.zeros(len(idx), int); k = 0
        for m in range(1, len(idx)):
            if idx[m] - idx[m - 1] > 48:
                k += 1
            ev[m] = k
        sub["event"] = [f"{sym}-{e}" for e in ev]
        sub["y_top"] = 0; sub["y_dump"] = 0
        for _, grp in sub.groupby("event"):
            gt = grp[grp["top_good"] == 1]
            if len(gt):
                sub.loc[gt["ts"].idxmin(), "y_top"] = 1
            gd = grp[grp["dump_good"] == 1]
            if len(gd):
                sub.loc[gd["ts"].idxmin(), "y_dump"] = 1
        rows.append(sub)
    return pd.concat(rows, ignore_index=True), start, end


def fit_model(d, ycol, feats=None):
    feats = feats or FEATS
    ts = d.ts.values
    cut = np.quantile(ts, 0.80)
    tr, va = ts < cut, ts >= cut
    pos = d[ycol][tr].sum(); neg = tr.sum() - pos
    params = dict(objective="binary", n_estimators=300, learning_rate=0.03, num_leaves=32,
                  min_child_samples=60, subsample=0.8, colsample_bytree=0.7, reg_lambda=1.0,
                  scale_pos_weight=max(1.0, neg / max(pos, 1)), n_jobs=-1, verbosity=-1)
    m = lgb.LGBMClassifier(**params); m.fit(d.loc[tr, feats], d.loc[tr, ycol])
    va_auc = float("nan")
    if va.sum() and d.loc[va, ycol].nunique() > 1:
        va_auc = float(roc_auc_score(d.loc[va, ycol], m.predict_proba(d.loc[va, feats])[:, 1]))
    # 最终在全部数据上重训
    pos = d[ycol].sum(); neg = len(d) - pos
    params["scale_pos_weight"] = max(1.0, neg / max(pos, 1))
    final = lgb.LGBMClassifier(**params); final.fit(d[feats], d[ycol])
    scores = final.predict_proba(d[feats])[:, 1]
    thr = float(np.quantile(scores, 0.95))       # top5% 作为触发阈值
    thr_high = float(np.quantile(scores, 0.98))   # top2% 作为高置信
    return final.booster_, va_auc, thr, thr_high, int(pos)


def _data_end(source):
    end = 0
    files = [f for f in glob.glob(os.path.join(source, "klines", "*.parquet"))
             if os.path.basename(f)[:-8].upper() not in EXCLUDE][:5]
    for f in files:
        try:
            pf = pq.ParquetFile(f)
            for i in range(pf.metadata.num_row_groups):
                st = pf.metadata.row_group(i).column(0).statistics
                if st and st.has_min_max:
                    end = max(end, int(st.max))
        except Exception:
            pass
    return end or int(datetime.now(timezone.utc).timestamp() * 1000)


def _read_ms(source, group, sym, cols, start, end):
    p = os.path.join(source, "market_state_hist", group, f"{sym}.parquet")
    if not os.path.exists(p):
        return None
    try:
        return (pq.read_table(p, columns=["timestamp"] + cols, filters=[("timestamp", ">=", start - 3 * DAY), ("timestamp", "<=", end)])
                .to_pandas().drop_duplicates("timestamp").sort_values("timestamp"))
    except Exception:
        return None


def _flow_frame(source, sym, g, start, end):
    f = pd.DataFrame({"b": g["b"].values, "close": g["close"].values})
    for grp, cols, names in [("oi", ["oi", "oi_value"], ["oi", "oival"]), ("global_acct_ratio", ["ratio"], ["lsg"]),
                             ("top_pos_ratio", ["ratio"], ["lstp"]), ("taker_ratio", ["ratio"], ["tkr"])]:
        d = _read_ms(source, grp, sym, cols, start, end)
        if d is None:
            for nn in names:
                f[nn] = np.nan
        else:
            mm = pd.merge_asof(f[["b"]], d, left_on="b", right_on="timestamp", direction="backward")
            for c, nn in zip(cols, names):
                f[nn] = mm[c].values
    return f


def build_long_rows(source, days):
    end = _data_end(source); start = end - days * DAY
    files = [f for f in glob.glob(os.path.join(source, "klines", "*.parquet"))
             if os.path.basename(f)[:-8].upper() not in EXCLUDE]
    G = {}; SYMc = []; TS = []; QV = []
    for path in files:
        sym = os.path.basename(path)[:-8].upper()
        try:
            g = agg15(path, start - 3 * DAY, end)
        except Exception:
            continue
        if g is None or len(g) < LOOKBACK + 300:
            continue
        G[sym] = g; SYMc.append(sym)
        TS.append(g.b.values); QV.append(g.qv.rolling(2).sum().values)
    Q = pd.DataFrame({"ts": np.concatenate(TS), "qv": np.concatenate(QV)})
    Q["rank"] = Q.groupby("ts")["qv"].rank(ascending=False, method="min")
    rank_all = Q["rank"].values; pos = 0; RANK = {}
    for sym in SYMc:
        n = len(G[sym]); RANK[sym] = rank_all[pos:pos + n]; pos += n
    rows = []
    for sym in SYMc:
        g = G[sym]; F = compute_features(g); c, h, l = g.close, g.high, g.low; n = len(g)
        FL = compute_flow_features(_flow_frame(source, sym, g, start, end))
        ret2 = c / c.shift(2) - 1; ret16 = c / c.shift(16) - 1; ret48 = c / c.shift(48) - 1; ret96 = c / c.shift(96) - 1
        qv30 = g.qv.rolling(2).sum(); volr30 = qv30 / qv30.rolling(20).mean()
        breakout = (c > np.maximum(g.open, c).rolling(8).max().shift(1)).values
        inpump = ((ret16 >= PUMP_4H) | (ret48 >= PUMP_12H) | (ret96 >= PUMP_1D)).values
        cand = ((ret2 >= LONG_RET2_MIN) & (volr30 >= LONG_VOLR30_MIN) & (ret96 <= LONG_HEAT_24H) & (ret16 <= LONG_HEAT_4H)
                & (ret48 <= LONG_HEAT_12H) & breakout & (F["close_pos"] >= LONG_CPOS_MIN) & (F["uwick"] <= LONG_UWICK_MAX)
                & (F["dist_ema21"] > 0) & (F["dist_ema21"] <= LONG_DIST_EMA21_MAX) & (F["ema_spread"] > 0)
                & (~pd.Series(inpump, index=F.index)) & (pd.Series(RANK[sym], index=F.index) <= 150)).values
        valid = np.zeros(n, bool); valid[LOOKBACK:max(LOOKBACK, n - 289)] = True
        finite = np.isfinite(c.values) & F[FEATS].notna().all(axis=1).values
        idx = np.where(cand & valid & finite)[0]
        if len(idx) == 0:
            continue
        ylong = np.array([1 if any(inpump[j] for j in range(i + 1, min(i + 193, n))) else 0 for i in idx])
        sub = pd.concat([F.iloc[idx][FEATS].reset_index(drop=True), FL.iloc[idx][flow_columns()].reset_index(drop=True)], axis=1)
        sub["y_long"] = ylong; sub["ts"] = g.b.values[idx]; sub["symbol"] = sym
        ev = np.zeros(len(idx), int); k = 0
        for m in range(1, len(idx)):
            if idx[m] - idx[m - 1] > 96:
                k += 1
            ev[m] = k
        sub["event"] = [f"{sym}-{e}" for e in ev]
        rows.append(sub)
    return pd.concat(rows, ignore_index=True), start, end


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.environ.get("HUNTER_BB_SOURCE", ""), help="parquet 数据根目录(含 klines/)")
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args(argv)
    if not args.source or not os.path.isdir(os.path.join(args.source, "klines")):
        raise SystemExit("请用 --source 指定含 klines/ 的 parquet 目录, 或设 HUNTER_BB_SOURCE 环境变量")
    print(f"训练数据: {args.source}  近 {args.days} 天", flush=True)
    df, start, end = build_rows(args.source, args.days)
    print(f"候选={len(df)} 事件={df.event.nunique()} 币={df.symbol.nunique()}", flush=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_start": datetime.fromtimestamp(start / 1000, timezone.utc).isoformat(timespec="seconds"),
        "data_end": datetime.fromtimestamp(end / 1000, timezone.utc).isoformat(timespec="seconds"),
        "days": args.days, "n_symbols": int(df.symbol.nunique()), "n_candidates": int(len(df)),
        "feature_cols": FEATS,
    }
    for task, setup, ycol in (("dump", "dump_setup", "y_dump"), ("top", "top_setup", "y_top")):
        d = df[df[setup] == 1].reset_index(drop=True)
        booster, auc, thr, thr_high, npos = fit_model(d, ycol)
        booster.save_model(str(MODELS_DIR / f"{task}.txt"))
        meta[task] = {"val_auc": round(auc, 3), "n_pos": npos, "n_cand": int(len(d)), "thr": round(thr, 4), "thr_high": round(thr_high, 4)}
        print(f"[{task}] val_auc={auc:.3f} 正例={npos} 阈值={thr:.3f}/{thr_high:.3f}", flush=True)

    # 做多模型(base + 资金流特征, 标签=48h 内成妖币)
    LFEATS = long_feature_columns()
    dfl, _, _ = build_long_rows(args.source, args.days)
    dl = dfl.reset_index(drop=True)
    booster, auc, thr, thr_high, npos = fit_model(dl, "y_long", feats=LFEATS)
    booster.save_model(str(MODELS_DIR / "long.txt"))
    meta["long_feature_cols"] = LFEATS
    meta["long"] = {"val_auc": round(auc, 3), "n_pos": npos, "n_cand": int(len(dl)), "thr": round(thr, 4), "thr_high": round(thr_high, 4)}
    print(f"[long] val_auc={auc:.3f} 正例={npos} 候选={len(dl)} 阈值={thr:.3f}/{thr_high:.3f}", flush=True)

    (MODELS_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已保存 ml/models/{dump.txt,top.txt,long.txt,meta.json}", flush=True)


if __name__ == "__main__":
    main()
