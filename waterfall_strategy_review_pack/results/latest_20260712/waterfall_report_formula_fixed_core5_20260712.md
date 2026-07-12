# Waterfall Pattern Discovery

Events: 119349 across 500 symbols

## Event Families

| family | events | symbols | median_drop_30m | median_adverse_5m | median_ret_24h | median_runup_24h |
| --- | --- | --- | --- | --- | --- | --- |
| range_breakdown | 54706 | 499 | 3.791 | 0.232 | 1.151 | 7.438 |
| other | 36776 | 461 | 3.952 | 0.459 | 5.708 | 15.776 |
| post_pump | 21646 | 319 | 4.634 | 0.663 | 39.899 | 54.196 |
| downtrend_continuation | 4467 | 244 | 5.012 | 0.535 | -13.459 | 4.231 |
| momentum_dump | 1754 | 264 | 5.133 | 0.592 | 2.003 | 16.455 |

Trades: 1555 across 236 symbols

## Rule Results

| rule | trades | symbols | win_rate | avg_ret | median_ret | profit_factor | median_mae | p80_mae | median_mfe | p80_mfe | big3_rate | big5_rate | big10_rate | loss5_rate | median_hold_min |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| robust_downtrend_range_flush_1m | 235 | 138 | 56.17 | 4.875 | 2.623 | 4.363 | 2.311 | 5.024 | 3.963 | 12.131 | 45.532 | 36.596 | 19.149 | 0.0 | 2.0 |
| robust_downtrend_upper_break_1m | 386 | 173 | 52.591 | 3.271 | 2.359 | 3.274 | 2.258 | 4.825 | 3.619 | 9.321 | 38.86 | 26.943 | 14.249 | 0.0 | 2.0 |
| robust_other_pullback_dump_1m | 187 | 80 | 58.289 | 0.872 | 0.697 | 1.811 | 1.757 | 3.534 | 3.317 | 4.748 | 26.738 | 7.487 | 1.07 | 0.0 | 13.0 |
| robust_momentum_uptrend_dump_1m | 227 | 115 | 58.59 | 0.772 | 0.625 | 1.694 | 1.628 | 3.6 | 3.298 | 4.63 | 24.67 | 9.251 | 1.322 | 0.0 | 7.0 |
| robust_post_pump_red_sell_1m | 520 | 129 | 52.308 | 0.531 | 0.313 | 1.398 | 1.977 | 3.802 | 3.13 | 4.837 | 26.923 | 8.846 | 1.346 | 0.0 | 5.0 |

## Rule / Family Results

| rule_family | trades | symbols | win_rate | avg_ret | median_ret | profit_factor | median_mae | p80_mae | median_mfe | p80_mfe | big3_rate | big5_rate | big10_rate | loss5_rate | median_hold_min |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| robust_downtrend_range_flush_1m/downtrend_continuation | 235 | 138 | 56.17 | 4.875 | 2.623 | 4.363 | 2.311 | 5.024 | 3.963 | 12.131 | 45.532 | 36.596 | 19.149 | 0.0 | 2.0 |
| robust_downtrend_upper_break_1m/downtrend_continuation | 386 | 173 | 52.591 | 3.271 | 2.359 | 3.274 | 2.258 | 4.825 | 3.619 | 9.321 | 38.86 | 26.943 | 14.249 | 0.0 | 2.0 |
| robust_other_pullback_dump_1m/other | 187 | 80 | 58.289 | 0.872 | 0.697 | 1.811 | 1.757 | 3.534 | 3.317 | 4.748 | 26.738 | 7.487 | 1.07 | 0.0 | 13.0 |
| robust_momentum_uptrend_dump_1m/momentum_dump | 227 | 115 | 58.59 | 0.772 | 0.625 | 1.694 | 1.628 | 3.6 | 3.298 | 4.63 | 24.67 | 9.251 | 1.322 | 0.0 | 7.0 |
| robust_post_pump_red_sell_1m/post_pump | 520 | 129 | 52.308 | 0.531 | 0.313 | 1.398 | 1.977 | 3.802 | 3.13 | 4.837 | 26.923 | 8.846 | 1.346 | 0.0 | 5.0 |
