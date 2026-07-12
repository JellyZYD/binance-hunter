# ML experiment notes

Run date: 2026-07-01

## Dataset

Full dataset:

- source: `E:\A\币安数据库`
- symbols: 171 altcoin USDT perpetual contracts
- samples: 75,262 watch-state 15m sequences
- sequence length: 64 closed 15m bars
- features: 26 price/volume/taker/state features
- `top` positive rate: 32.15%
- `dump` positive rate: 20.48%

Pilot dataset:

- symbols: 79
- samples: 17,091
- same feature and label definitions as full dataset

## Best Current Candidates

For top/early warning, use `gru_stack` first:

- full test AUC: 0.564
- full test AP: 0.353
- full test P@20: 70%
- full test P@50: 66%
- full test top 1% precision: 49.1%
- full test top 5% precision: 35.5%

`gru_medium` is the lighter fallback:

- full test AUC: 0.557
- full test AP: 0.353
- full test P@20: 50%
- full test P@50: 46%
- full test top 1% precision: 44.6%
- full test top 5% precision: 40.2%

For dump/downmove-start prediction, do not replace rules with ML yet:

- full `gru_small` test AUC: 0.538
- full `gru_small` test AP: 0.230
- full `gru_small` test P@50: 30%
- full `gru_medium` test AUC: 0.534
- full `gru_medium` test AP: 0.231
- full `gru_medium` test P@50: 24%

## Interpretation

The ML signal is more useful for ranking possible topping/early-warning moments
than for independently detecting the downmove-start label.

Use it as an auxiliary score:

- `top` model: raise early-warning priority when the rule engine already sees a
  pump and consolidation/rejection setup.
- `dump` model: only use as a weak filter; rule-based 15m break plus volume
  confirmation remains the primary short trigger.

Avoid using wider TCN/Transformer variants as defaults. In pilot runs they often
looked strong on validation but fell apart on the later test split, while taking
longer to train and infer.
