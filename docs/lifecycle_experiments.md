# 生命周期实验

本文件记录当前已落地生产的生命周期专家版结论。

## 目标

不要把所有做多、见顶、做空信号混在一个大模型里。更合理的结构是：

1. 先识别可能进入目标拉升生命周期的做多启动；
2. 所有已经明显拉升的币都进入 PumpWatch；
3. 随 K 线变化动态判断当前处于哪个生命周期阶段；
4. 对不同阶段和类型使用不同专家模型。

## 动态阶段

生产使用规则路由器 `dyn_big_pump_tolerant`：

- `acceleration`
- `trend_hold`
- `distribution`
- `climax_risk`
- `pullback_risk`
- `breakdown`
- `neutral_watch`

实盘只把阶段作为路由和展示信息，不在早期强行硬分类“庄家类型”。

## 专家设计

`fast_dump`：

- 目标是快拉后急跌；
- `fast_top` 用于顶部/平多预警；
- `fast_short` 用于下跌启动做空提示。

`slow_distribution`：

- 目标是高位横盘/派发后破位；
- `slow_warning` 是高位风险预警；
- `slow_short` 才是做空提示。

## 周期实验

对比过 15m 与 5m 顶部/做空专家。5m 更灵敏，但主策略整体质量低于 15m，尤其 short 的噪音和信号稀疏问题更明显。最终生产选择：

- long：5m native；
- top/short：15m；
- 同类信号冷却：2h。

## 冷却实验

15m 生命周期专家多次信号：

| 版本 | 信号数 | 6h 跌幅中位 | 24h 跌幅中位 | 72h 跌幅中位 | 24h 逆向中位 | clean big short |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1h 冷却 | 62 | 6.7% | 17.7% | 21.0% | 4.3% | 37.1% |
| 2h 冷却 | 42 | 8.2% | 21.7% | 61.8% | 4.6% | 38.1% |
| 4h 冷却 | 29 | 10.9% | 21.2% | 58.4% | 4.4% | 37.9% |

2h 版本在信号数量和大肉覆盖之间更均衡，因此作为生产默认。

## 工程落地

生产已完成：

- `backend/pump_dump_hunter/ml/lifecycle.py`：生命周期特征和动态路由；
- `backend/pump_dump_hunter/ml/models/lifecycle/`：生产模型；
- `SignalEngine`：`strategy_version="lifecycle_expert"` 分支；
- `settings.json`：5m/15m WebSocket、2h 冷却；
- SQLite：PumpEvent/Alert/LongEvent lifecycle 字段；
- 前端：展示当前周期、类型、阶段、模型和分数；
- 企业微信/Markdown：信号推送带 lifecycle 元数据。
