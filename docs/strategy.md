# Strategy Notes

当前策略目标是抓“短时间暴涨或多周期大涨后的高位放量转弱”，只提示做空，不自动下单。

## 事件一致性

实盘和回测共用 `SignalEngine`：

- 实盘：WebSocket 收到 Binance kline `x=true` 后生成 `KlineClosed`。
- 回测：SQLite 历史 K 线按时间顺序 replay 同样的 `KlineClosed`。
- Discovery：live/backtest 都只能使用当时已经收线的数据窗口。

这样避免“回测看完整 K 线、实盘看未完成 K 线”的偏差。

## 入池逻辑

入池不是做空信号，只是进入盯盘：

- 15m 涨幅达到强拉阈值。
- 30m 涨幅达到强拉阈值。
- 4h/12h/1d 达到妖币大级别涨幅。
- 或者 15m/30m 涨幅排名靠前，且伴随成交额放大。

当前默认阈值在 `backend/config/settings.json`：

- `pump_15m_pct = 8`
- `pump_30m_pct = 10`
- `pump_4h_pct = 20`
- `pump_12h_pct = 30`
- `pump_1d_pct = 40`

## 信号逻辑

`early_alert`：

- 使用 15m 已收线 K 线。
- 高位 3 根左右横盘。
- 出现向上插针回落，或一根大阳后一根低收阴线的两根 K 线冲高回落。
- 量比达到阈值，且距离起涨锚点仍有下跌空间。

`short_signal`：

- 使用 15m 已收线 K 线。
- 从高点回落达到阈值。
- 放量。
- 跌破 EMA21 或跌破 15m 实体结构低点。
- 仍有足够回落空间。

结构低点使用实体低点 `min(open, close)`，不使用插针最低点。

## 后续扩展

项目后续可以新增一个独立 profile 做“妖币上涨趋势追随做多”，建议复用事件模型和数据库，新增：

- `long_pump_events`
- `long_alerts` 或在 `alerts.level` 中扩展 long signal level
- 独立 `SignalParams` profile
- 同样坚持只用已收线数据生成正式信号
