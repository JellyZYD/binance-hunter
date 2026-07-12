# Long Entry Pool Validation

Run date: 2026-07-01

## Scope

- source: `E:\A\币安数据库`
- period: 2025-06-13 19:00 to 2026-06-14 00:00 local time
- symbols: 173 altcoin USDT perpetual contracts
- rows: 5,325,916 closed 15m bars
- target events: 1,100

Target definition:

- entry bar is not already in the short-pool state;
- within the next 48h, the symbol enters the short-pool pump state:
  - 4h gain >= 20%, or
  - 12h gain >= 30%, or
  - 24h gain >= 40%.

Entries are deduplicated with a 24h cooldown per symbol.

## Recommended Pool

Use this as the default long watchlist entry rule:

- 30m quote-volume rank <= 150 among altcoin contracts;
- 30m close return >= 4.5%;
- 30m quote volume >= 2.0x its prior rolling baseline;
- 24h return <= 25%;
- 4h return <= 18%;
- 12h return <= 28%;
- 15m close breaks the prior 8-bar body high;
- close position >= 0.60;
- upper wick <= 6%;
- close is above EMA21 but no more than 12% above it;
- EMA8 > EMA21;
- not already in the short-pool state.

Validation:

- events: 3,226
- hits: 721
- precision: 22.35%
- target recall: 57.91%
- events/day: 8.9
- median entry 24h gain: 7.41%
- median entry 4h gain: 7.27%
- median future high gain: 8.45%
- median adverse drawdown: 9.45%
- median lead time before short-pool entry: 2.75h

## Tiers

Broad scout tier:

- useful when the goal is to watch more early movers;
- captures 80.7% of target events but creates 37.7 entries/day;
- precision is only 8.4%.

Balanced tier:

- manually designed rule with 4% 30m or 6% 1h momentum, 2.5x volume, 16-bar body breakout;
- 4,148 events, 18.5% precision, 62.3% target recall, 11.5 entries/day.

Strict tier:

- 5% 30m or 7% 1h momentum, 3x volume, tighter wick and heat limits;
- 1,904 events, 20.2% precision, 34.1% target recall, 5.3 entries/day.

Grid-selected default:

- 4.5% 30m momentum, 2x volume, 8-bar body breakout, 24h <= 25%;
- 3,226 events, 22.35% precision, 57.9% target recall, 8.9 entries/day.

## Interpretation

The best entry pool is not the earliest possible detector. Very early volume
rules catch many future pumps but are too noisy. The useful zone is after the
first meaningful 30m thrust, while the 24h/4h heat is still below the existing
short-pool thresholds.

This should be used as a watchlist/candidate pool, not as an automatic buy
signal. The next step is to train or validate a long-start model on this
candidate set, then emit long signals only for high-scoring candidates.
