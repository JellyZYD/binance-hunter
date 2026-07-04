# 策略说明

当前生产默认是 **生命周期专家版**，只发人工参考信号，不自动下单。

核心流程：

1. REST discovery 每 15m 扫描 USDT 永续山寨币，排除 BTC/ETH/主流币/股票/贵金属合约。
2. 流动性 TopN 进入 WebSocket，实盘只处理 Binance kline `x=true` 的已收线 K 线。
3. 两条入口并行：
   - `LongEvent`：可能进入拉升生命周期的做多监测池。
   - `PumpEvent`：已经明显拉升的顶部/做空监测池，即使没有触发做多信号也会进入。
4. 做多信号用 5m 生命周期 long 模型。
5. 顶部预警和下跌启动用 15m 生命周期专家模型。

## 当前生产配置

配置文件：`backend/config/settings.json`

| 项 | 当前值 |
| --- | --- |
| `signals.mode` | `ml` |
| `signals.strategy_version` | `lifecycle_expert` |
| WebSocket 周期 | `5m`, `15m` |
| 做多信号周期 | `5m` |
| 顶部/做空信号周期 | `15m` |
| 同类信号冷却 | `2h` |
| 正式信号数据 | 只用已收线 K 线 |

## 入池

`discovery.py` 仍先用 REST 做横截面筛选：

- 扫描 24h 成交额 broad pool；
- 拉最近 1m K 线计算 15m/30m 成交额、涨幅、振幅、量比；
- 计算 4h/12h/1d 大级别涨幅；
- 满足明显拉升条件的币进入 PumpWatch 影子观察；
- 生命周期最高涨幅达到 `25%` 后进入正式 PumpWatch，才允许顶部/做空专家发信号；
- 生命周期最高涨幅达到 `40%` 后启用 high-pump 特殊顶部/破位专家；
- 满足早期拉升候选条件的币进入 LongWatch。

PumpWatch 影子观察只更新锚点和高点，不是正式监管，也不会发顶部/做空信号。正式 PumpWatch 是“顶部/做空监控”，仍然不是做空信号。LongWatch 是“可能做多监控”，不是做多信号。

## 做多信号

做多信号使用 5m native 生命周期 long 模型：

- 模型：`long_pump_event.txt` + `long_start_quality.txt`
- 组合权重：`0.65 * pump_event + 0.35 * start_quality`
- 触发阈值：q90 `0.700658`
- 高置信阈值：q95 `0.764311`

做多信号触发后，会同时建立/激活同币 `PumpEvent`，用于后续顶部预警和下跌启动监控。

## 生命周期状态

每根 15m 收线后，PumpWatch 会更新动态状态：

- `acceleration`：加速拉升；
- `trend_hold`：趋势保持；
- `distribution`：高位派发；
- `climax_risk`：冲顶风险；
- `pullback_risk`：回落风险；
- `breakdown`：破位；
- `neutral_watch`：中性观察。

生产信号只在最佳回测选定的风险状态触发：

`distribution`, `climax_risk`, `pullback_risk`

这点要和回测保持一致，不在生产里额外放开 `breakdown`。

## 顶部/做空专家

15m 生命周期专家模型：

| 模型 | 类型 | 信号 | 阈值 |
| --- | --- | --- | ---: |
| `fast_top` | `fast_dump` | `early_alert` | `0.523018` |
| `fast_short` | `fast_dump` | `short_signal` | `0.620131` |
| `slow_warning` | `slow_distribution` | `early_alert` | `0.794619` |
| `slow_short` | `slow_distribution` | `short_signal` | `0.534692` |

`fast_dump` 更偏快拉急跌，`slow_distribution` 更偏高位派发后慢慢破位。`slow_warning` 是高位风险/平多预警，不应理解成强做空点。

## 输出字段

每条正式信号都会落库并推送：

- `lifecycle_mode`：`fast_dump` / `slow_distribution` / `long_entry` / `trend_watch`；
- `behavior_state`：当前动态阶段；
- `model_name`：触发模型；
- `model_score` / `model_threshold`；
- `signal_interval`：`5m` 或 `15m`；
- `occurrence`：同一 PumpEvent 同类信号第几次。

前端“合约主力动向监控”会在正在监控的合约旁边显示当前类型和阶段，信号卡片显示模型、周期和分数。
