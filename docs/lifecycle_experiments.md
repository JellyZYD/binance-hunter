# 生命周期专家策略说明

本文记录当前生产版 `lifecycle_expert` 的策略结构、实盘入口和风控边界。

## 目标

不要把做多、见顶、做空信号混在一个大模型里。当前结构是：

1. `LongWatch`：扫描可能启动上涨的币，5m 收线后由做多启动模型给出做多观察信号。
2. `PumpWatch`：所有已经明显拉升的币先进入影子观察，即使没有触发做多信号也要跟踪；生命周期最高涨幅达到正式门槛后才进入顶部/做空监管。
3. 生命周期路由：用已收线 K 线动态判断 `acceleration / trend_hold / distribution / climax_risk / pullback_risk / breakdown`。
4. 专家模型：不同阶段使用 `fast_dump` 或 `slow_distribution` 的 top/short 专家。

## 当前生产配置

- 做多信号：`5m` 已收线，`long_pump_event + long_start_quality` 组合分。
- 顶部/做空信号：`15m` 已收线，生命周期专家模型。
- 同一 PumpWatch 同类 top/short 信号冷却：`2h`。
- 同一 LongWatch 做多信号冷却：`2h`，避免 5m 每根 K 线重复刷屏。
- 普通 PumpWatch 正式监管门槛：生命周期最高涨幅至少 `25%`；低于该值只做 `shadow_watch`，不显示在主监控面板，也不调用 top/short 专家。
- 做多信号派生的 PumpWatch 仍使用 `15%` 门槛，用于做多后的平多/转空监控。
- high-pump 特殊专家门槛：生命周期最高涨幅至少 `40%`。
- 由做多信号派生的 PumpWatch，必须自做多监管价起至少涨过 `15%`，才允许进入 top/short 专家模型。
- PumpWatch 跌回起涨区后踢出监管池：当 `remaining_to_anchor < 5%` 且该事件曾经涨过至少 `8%`，标记 `status=closed`、`lifecycle_mode=completed`，从活跃监控和下一轮 WebSocket 订阅中移除。

## 专家模型

`fast_dump`：

- 目标是快拉后的急跌。
- `fast_top` 用于顶部/平多预警。
- `fast_short` 用于下跌启动做空提示。

`slow_distribution`：

- 目标是高位横盘、派发后破位。
- `slow_warning` 是高位风险预警，不追求精确尖顶。
- `slow_short` 才是做空提示。

## 5m 与 15m 对照

对比过 15m 与 5m 顶部/做空专家。5m 更灵敏，但主策略整体质量低于 15m，short 噪音更明显。最终生产选择：

- `long`：5m native；
- `top/short`：15m；
- `top/short` 冷却：2h。

## 冷却实验

15m 生命周期专家多次信号：

| 版本 | 信号数 | 6h 跌幅中位 | 24h 跌幅中位 | 72h 跌幅中位 | 24h 逆向中位 | clean big short |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1h 冷却 | 62 | 6.7% | 17.7% | 21.0% | 4.3% | 37.1% |
| 2h 冷却 | 42 | 8.2% | 21.7% | 61.8% | 4.6% | 38.1% |
| 4h 冷却 | 29 | 10.9% | 21.2% | 58.4% | 4.4% | 37.9% |

2h 版本在信号数量和大肉覆盖之间更均衡，因此作为生产默认。

## 实盘边界

- 做多和做空监控分离：做多信号不是进入做空监管的唯一入口。
- `long_signal -> PumpWatch` 只是为了后续生命周期跟踪；没涨够前不允许触发 top/short。
- 已经跌回起涨区的 PumpWatch 直接踢出监管池，防止已经打完的旧事件在冷却后继续发空。
- 前端 `/api/pumps` 默认只展示正式 active 监管；`/api/pumps?include_shadow=1` 可排查影子观察池；`/api/pump-history` 和 `/api/long-history` 展示历史监管记录。

## 工程落地

- `backend/pump_dump_hunter/ml/lifecycle.py`：生命周期特征和动态路由。
- `backend/pump_dump_hunter/ml/models/lifecycle/`：生产模型。
- `SignalEngine`：`strategy_version="lifecycle_expert"` 分支。
- `settings.json`：5m/15m WebSocket、冷却、成熟度和踢出阈值。
- SQLite：`PumpEvent / Alert / LongEvent` lifecycle 字段和做多信号时间字段。
- 前端：活跃合约卡片、LongWatch、监管历史记录、生命周期类型/阶段/模型分数。
- 企业微信/Markdown：信号推送带 lifecycle 元数据。
