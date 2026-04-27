

## Data

Pulled from FRED (2010–present, monthly):
- **VIX** (`VIXCLS`) — implied vol from S&P 500 options
- **EPU** (`USEPUINDXD`) — Baker/Bloom/Davis Economic Policy Uncertainty index
- **S&P 500** (`^GSPC` via yfinance) — daily close, resampled to month-end

All series aligned to month-end. Returns computed as % change. VIX and EPU
converted to first differences (diffs) to make them stationary.

## Plots

1. **Time series of monthly diffs** for VIX, EPU, and SPX returns — Big features land where they should: COVID 2020, Fed cycle 2022, elections 2024
   tariff shock spring 2025.
2. **Rolling 1Y annualized SPX return**
3. **Scatter: SPX returns vs VIX diff** — to test contemporaneous correlation.
4. **Scatter: SPX returns vs EPU diff** — same test for EPU.
5. **Scatter: |SPX returns| vs EPU diff** — to test whether EPU predicts
   *magnitude* of moves, even if not direction.

## Findings

### 1. VIX is essentially mechanical with SPX
Strong negative correlation (~-0.8) between VIX changes and SPX returns. Tight
diagonal cloud on the scatter

### 2. EPU has near-zero predictive power over SPX returns
Correlation between EPU diffs and SPX returns is roughly -0.10 — a barely-
visible downward tilt on the scatter, mostly a blob. With ~190 monthly
observations, this is borderline noise. **News-based aggregate economic policy
uncertainty does not meaningfully predict broad market direction at monthly
frequency.**

### 3. EPU doesn't clearly predict return magnitude either
Tested EPU diff vs |SPX return| (the "does uncertainty predict bigger moves?"
hypothesis). Slope is slightly positive but the cloud is huge and the
confidence band crosses zero. Effect, if it exists, is small.

## Stack

Python, pandas, fredapi, yfinance, matplotlib, seaborn.