# ML 模型

服务器只做推理，训练在本地完成后提交模型文件。

## 生产模型目录

全局旧模型仍保留在：

`backend/pump_dump_hunter/ml/models/`

生命周期专家版新增：

`backend/pump_dump_hunter/ml/models/lifecycle/`

包含：

- `long_pump_event.txt`
- `long_start_quality.txt`
- `fast_top.txt`
- `fast_short.txt`
- `slow_warning.txt`
- `slow_short.txt`
- `meta.json`

`MLScorer.lifecycle_ready=True` 才表示生命周期专家模型完整可用。

## 当前模型版本

`meta.json` 记录：

- 策略版本：`lifecycle_expert`
- 做多周期：`5m`
- 顶部/做空周期：`15m`
- 冷却：`2h`
- 数据区间：`2025-06-13` 到 `2026-06-13`
- 样本币数：172

## 推理链路

做多：

1. discovery 产生 LongWatch 候选；
2. WebSocket 收到 5m `x=true`；
3. 使用已收线 5m K 线计算生命周期 long 特征；
4. `long_pump_event` 和 `long_start_quality` 加权；
5. 分数达到 q90 发 `long_signal`。

顶部/做空：

1. PumpWatch 每根 15m `x=true` 更新 lifecycle row；
2. 动态路由器给出 `behavior_state`；
3. 只在 `distribution/climax_risk/pullback_risk` 状态下评估专家；
4. 分数超过对应阈值后发 `early_alert` 或 `short_signal`。

## 最佳回测摘要

### 5m 做多

holdout q90：

- 信号数：40
- 进入目标生命周期率：57.5%
- 做多启动成功率：55.0%
- 48h 最大上涨中位：17.9%
- 入场后先逆向中位：4.7%

### 15m 生命周期专家，2h 冷却

PumpWatch short 汇总：

- 信号数：42
- 6h 下跌中位：8.2%
- 24h 下跌中位：21.7%
- 72h 下跌中位：61.8%
- 24h 逆向中位：4.6%
- clean big short：38.1%

因此生产采用 **5m long + 15m expert + 2h cooldown**。

## 服务器更新

服务器执行：

```bash
cd /opt/binance-hunter
sudo bash deploy/update.sh
```

更新后检查：

```bash
journalctl -u binance-hunter-monitor -n 80 --no-pager
```

日志中应看到：

```text
ML scorer ready=True
```

网页 `/api/hunter/model` 应返回 `lifecycle_ready: true`。
