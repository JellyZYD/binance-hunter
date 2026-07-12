# 2026-07-12 最新瀑布策略审查说明

这份说明是基于 Claude 审查意见修正后的最新研究成果。重点是：修正空单收益公式、修正 1m K 内 trailing stop 的同根未来函数风险，并重新跑了核心 1m 瀑布策略回测；同时拆分了 aggTrade 的 `closed_1m` 与 `preclose_agg` 两种任务，避免把完整 1m K 线信息误用于提前触发模型。

## 1. 已按审查意见修正的问题

### 空单收益公式

之前部分脚本使用了：

```text
entry / exit - 1
```

这对 USDT 线性合约空单不严格。现在统一改成：

```text
1 - exit / entry
```

已修正脚本：

- `code/latest_20260712/discover_waterfall_patterns.py`
- `code/latest_20260712/compare_waterfall_replay_modes.py`
- `code/latest_20260712/optimize_waterfall_exit_profiles.py`
- `code/latest_20260712/waterfall.py`

### 同根 K 线 trailing stop 未来函数

之前同一根 1m K 内可能先用当前 low 更新 best/trail，再用当前 high 判断是否触发 trailing stop，这在 OHLC 回测里存在同根顺序不可知的问题。

现在改为更保守顺序：

1. 先用当前 high 判断固定止损。
2. 只用上一根已经确认的 trail 判断当前 high 是否触发 trailing stop。
3. 如果没有退出，再用当前 low 更新 best/trail，供下一根使用。

这会牺牲部分乐观收益，但更接近可执行回测。

## 2. 修正后核心 1m 策略结果

结果目录：

- `results/latest_20260712/waterfall_report_formula_fixed_core5_20260712.md`
- `results/latest_20260712/waterfall_summary_formula_fixed_core5_20260712.json`
- `results/latest_20260712/waterfall_trades_formula_fixed_core5_20260712.csv`

全样本：

| 指标 | 数值 |
|---|---:|
| 交易数 | 1555 |
| 覆盖币种 | 236 |
| 回测跨度 | 367 天 |
| 日均交易 | 4.24 |
| 胜率 | 54.60% |
| 平均收益 | 1.94% |
| 中位收益 | 0.57% |
| PF | 2.48 |
| median MAE | 1.98% |
| p80 MAE | 3.96% |
| 3%+ 大肉率 | 32.35% |
| 5%+ 大肉率 | 17.43% |

2026-04-01 之后 holdout：

| 指标 | 数值 |
|---|---:|
| 交易数 | 555 |
| 天数 | 80 |
| 日均交易 | 6.94 |
| 胜率 | 52.07% |
| 平均收益 | 0.84% |
| 中位收益 | 0.31% |
| PF | 1.62 |
| median MAE | 2.06% |
| p80 MAE | 3.98% |
| 3%+ 大肉率 | 29.55% |
| 5%+ 大肉率 | 12.61% |

recent90：

| 指标 | 数值 |
|---|---:|
| 交易数 | 592 |
| 日均交易 | 6.58 |
| 胜率 | 51.69% |
| 平均收益 | 0.79% |
| 中位收益 | 0.30% |
| PF | 1.57 |
| median MAE | 2.10% |
| p80 MAE | 3.99% |
| 3%+ 大肉率 | 28.89% |
| 5%+ 大肉率 | 12.16% |

更保守成本估算：

- 当前 CSV 里已按约 0.08% 一轮成本扣除。
- 如果按约 0.30% 一轮成本粗略扣除，holdout PF 约 1.42，recent90 PF 约 1.38。
- 这说明策略仍有边际，但没有旧报告看起来那么夸张，实盘滑点/手续费很关键。

## 3. 分类型结果

全样本：

| family | 交易数 | 胜率 | 平均收益 | 中位收益 | PF | median MAE | 5%+ |
|---|---:|---:|---:|---:|---:|---:|---:|
| downtrend_continuation | 621 | 53.95% | 3.88% | 2.43% | 3.69 | 2.27% | 30.60% |
| other | 187 | 58.29% | 0.87% | 0.70% | 1.81 | 1.76% | 7.49% |
| momentum_dump | 227 | 58.59% | 0.77% | 0.62% | 1.69 | 1.63% | 9.25% |
| post_pump | 520 | 52.31% | 0.53% | 0.31% | 1.40 | 1.98% | 8.85% |

近期/holdout 的主要变化：

- `downtrend_continuation` 全样本最强，但 2026 holdout 降到 PF 约 1.45，recent90 约 1.43。
- `other` 在 holdout/recent90 反而最好，PF 约 2.8-2.9，但交易数较少。
- `post_pump` 稳定但收益小，holdout PF 约 1.68，recent90 约 1.63。
- `momentum_dump` 可保留观察，但不是主力收益来源。

## 4. aggTrade 最新结论

### 为什么拆成两种模式

原始 agg 分类结果过好，审查后发现要严格区分：

1. `closed_1m`：完整 1m K 已收线后，agg 只作为过滤器。
2. `preclose_agg`：当前 1m 未收线，只能用本分钟已经发生到 cutoff 的 aggTrade。

不能用完整 1m K 的 `body_drop/drop_2m/drop_5m/close_pos/break_depth` 去训练提前进场模型，否则会把本分钟未来信息混进去。

### preclose_agg 结果

结果文件：

- `results/latest_20260712/agg_event_classifier_preclose_report_20260712.md`
- `results/latest_20260712/agg_event_classifier_preclose_summary_20260712.csv`

严格 preclose AUC：

| cutoff | AUC |
|---:|---:|
| 10s | 0.789 |
| 20s | 0.819 |
| 30s | 0.843 |
| 40s | 0.863 |
| 50s | 0.886 |
| 59s | 0.919 |

解释：

- aggTrade 对真假瀑布确实有信息。
- 越接近 1m 收线，效果越好。
- 但这仍是“已知它最终属于 broad 1m 候选”的事件集，不等于全市场实时提前触发回测。
- 不能直接把 precision 当成实盘胜率。

### closed_1m 结果

结果文件：

- `results/latest_20260712/agg_event_classifier_closed1m_report_20260712.md`
- `results/latest_20260712/agg_event_classifier_closed1m_summary_20260712.csv`

`closed_1m` AUC 接近 0.989，因为它可以使用完整收线后的 1m K 信息。这适合做信号过滤，不适合证明提前入场。

## 5. 尚未完成但已准备好的交易级 agg 管线

新增代码：

- `code/latest_20260712/build_agg_features_for_waterfall_trades.py`
- `code/latest_20260712/train_agg_waterfall_trade_filter.py`

目的：

- 不再用泛化的 true/fake event 标签。
- 直接基于核心 1m 策略的 1555 笔真实交易样本提取 agg 特征。
- 标签和评价直接看交易收益、PF、MAE、MFE，而不是只看“未来是否跌”。

当前状态：

- 已生成核心交易专用下载清单：`results/latest_20260712/core_trade_agg_download_jobs.csv`
- 这份清单包含 1048 个有效合约日左右，用于补齐 1555 笔核心交易对应的 aggTrade 原始文件。
- 下载/特征抽取还没有完成，因此还没有可信的“交易级 agg 专属过滤模型”结果。

## 6. 需要 Claude 重点审查的问题

1. 空单收益公式修正是否完整，是否仍有脚本漏用 `entry / exit - 1`。
2. 1m OHLC 内 trailing stop 的保守执行顺序是否合理。
3. 当前 `downtrend_continuation` 全样本很强但 holdout 变弱，是否存在时代/行情结构过拟合。
4. `other` 在近期最好但定义较杂，是否应该继续拆分成更明确的形态。
5. `post_pump` 是用户直觉最强的模式，但统计收益小，是否是入场过晚、止盈过早、还是筛选池不对。
6. `preclose_agg` 的事件集仍由 1m broad candidate 构造，是否会引入选择偏差；后续是否必须做全市场逐秒/逐笔 replay。
7. 交易级 agg 管线的设计是否比当前 true/fake event classifier 更贴近实盘目标。
8. 更保守手续费/滑点下，哪些 family 仍值得上线，哪些只适合作为观察信号。

## 7. 不建议现在直接上线的原因

- 核心 1m 策略在修正后仍有正边际，但 recent/holdout 明显比全样本弱。
- agg 专属交易级过滤还没有完整跑完。
- `preclose_agg` 有信息，但还没完成“真实提前入场 + 真实止损止盈”的全链路回测。
- 实盘滑点可能对 0.5%-1% 均值收益的 family 影响很大。

当前建议：

```text
先让 Claude 审查 formula-fixed core5 结果和交易级 agg 管线；
如果审查通过，再补齐 core_trade agg 下载；
然后跑 train_agg_waterfall_trade_filter.py；
最后只把能在 holdout/recent90 提升 PF 且降低 MAE 的 agg 过滤接入生产。
```
