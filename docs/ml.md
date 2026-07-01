# ML 打分模型

信号从"规则即发"升级为**两段式:规则出候选 → LightGBM 打分 → 超阈值才发**。经离线验证(纯 holdout、打乱标签验伪、事件级去重)证明比纯规则显著更好,尤其"下跌启动"。

## 为什么是两段式

对所有盯盘 K 打分噪声太大(前期实验 AUC≈0.55)。改为**只在结构候选上打分**后:

- **下跌启动**:holdout AUC **0.91**,top5% 精度 37%(基线 4.5%),逆向更低,发得早(距高点 ~6%)。
- **见顶**:holdout AUC **0.78**,精度 30%(基线 10%),贴峰值发,但逆向仍 ~19%(抄顶天生难)。
- OI/多空比/资金费率做过 ablation:**无 OOS 增益**,故不使用。

## 组成(`backend/pump_dump_hunter/ml/`)

| 文件 | 作用 |
| --- | --- |
| `features.py` | 85 个特征 + setup 候选判定。**训练与实盘共用同一份**(否则模型失效) |
| `train.py` | 本地训练管线:读 parquet → 候选/事件级标签 → 训两个 LGB → 存模型+元数据 |
| `model.py` | 推理封装(`MLScorer`):加载模型打分,缺模型/依赖时优雅降级 |
| `models/` | 提交进仓库的产物:`dump.txt`、`top.txt`、`meta.json`(特征列表、阈值、训练时间、数据起止、AUC) |

信号引擎 `signals.mode="ml"` 时:每根 15m 收线,在候选上算特征→模型打分→分数≥阈值发信号(`top5%` 阈值触发、`top2%` 标"高置信")。见顶→顶部预警,破位→下跌启动。**已删除回落兜底。**

## 本地训练 / 更新模型(服务器 2 核 2G 不训练)

训练只在本地跑(需要 parquet 全量数据 + 额外依赖):

```bash
cd backend
pip install -r requirements.txt -r requirements-train.txt
python -m pump_dump_hunter.ml.train --source "E:\2C2G\币安数据库" --days 365
```

- `--source` 指向含 `klines/` 的 parquet 目录;`--days` 默认 365(实测下跌启动 ~240 天到平台、见顶 ~290 天,取 365 稳妥)。
- 产物写到 `ml/models/`。确认后提交推送:

```bash
git add backend/pump_dump_hunter/ml/models
git commit -m "chore(ml): 更新模型"
git push
```

- 服务器 `sudo bash deploy/update.sh` 拉取新模型 + 重启,`journalctl -u binance-hunter-monitor` 会打印 `ML scorer ready=True ...`。

## 更新提醒

- 网页顶部显示模型**数据起止 + 训练时间 + 两个 AUC**。
- 模型数据**超过 30 天**会变黄条提醒"建议本地重训后 push 更新"。
- 建议**每月**本地重训一次推送。

## 阈值 / 回退

- 阈值在 `meta.json`(`thr`=触发、`thr_high`=高置信),由训练时的分数分位数确定。想更严就重训时调,或后续做成可配置。
- 若服务器 lightgbm 安装失败或模型缺失,`mode="ml"` 会**不发信号**并日志告警;临时可把 `backend/config/settings.json` 的 `signals.mode` 改回 `v2` 或 `legacy` 再重启。

详见 [strategy.md](./strategy.md)。
