# Polymarket Geopolitics — Insider Candidate Findings

**Cohort:** 24 wallets passing the tightest filter (≥5 prior related markets, ≥75% cross-market
accuracy, FIFO-profitable). Funding latency pulled from on-chain USDC deposits via Etherscan.

## Headline

Combining three independent signals — **fast funding** (deposit→first-trade latency),
**cross-market accuracy**, and **cheap early entries on markets that resolved their way** —
isolates a small set of wallets that look like genuine informed traders, not market makers.

- **6 of 24 wallets deposited and traded within 24 hours.** All 6 are profitable.
- **2 wallets cleared +$400K** in realized FIFO P&L on Iran-strike / ceasefire markets.
- Top candidates enter **cheap** (below $0.85) on the eventual winning side, **10–35 days
  before resolution** — not buying at $0.99 after the outcome is obvious.

## Funding latency table (sorted fastest first)

| wallet | latency | first deposit | prior mkts | accuracy | FIFO P&L |
|---|---|---|---|---|---|
| 0x92a6294c…118b84 | 0.0h | $1,499 | 11 | 91% | **+$210,426** |
| 0x73d225c8…34f9b7 | 0.0h | $11,997 | 9 | 100% | +$5,044 |
| 0xf243214f…835fb4 | 0.1h | $100 | 22 | 82% | +$1,487 |
| 0x60f72939…1c050b | 2.3h | $926 | 8 | 100% | +$5,109 |
| 0x413d1ff0…02d704 | 3.4h | $1,000 | 10 | 89% | +$2,345 |
| 0x88fda04f…c750e | 10.6h | $13,821 | 8 | 83% | +$521 |
| 0xa9964972…dd6e251 | 24.6h | $54 | 20 | 100% | −$2,068 |
| 0x8a480b60…a2558 | 41.4h | $10 | 22 | 100% | **+$402,033** |
| 0x641977cc…b5997 | 78.2h | $4,994 | 10 | 100% | +$2,475 |
| 0x7661306c…f208a | 94.3h | $1,999 | 5 | 100% | +$1,369 |
| 0xd48a81db…366d90 | 204.1h | $699 | 21 | 100% | **+$424,802** |

(13 slower-funding wallets omitted from the high-priority list.)

## Top 3 individual profiles (entry behavior on winning markets)

**0x92a6294c… — strongest combined signal.** Deposited $1,499 and traded the same hour.
91% accurate across 11 Iran/Israel clusters. 30 of 34 markets entered below $0.85.
Example: entered an Iran-military-action market at $0.02, 19.8 days before resolution. +$210K.

**0x8a480b60… — biggest winner on US-strikes-Iran timing markets.** 100% across 22 clusters,
+$402K. Concentrated in "Will the US next strike Iran on [date]" series. 29 of 33 markets
entered cheap; mix of early cheap entries and some late confirmation buys.

**0xd48a81db… — cleanest predictive pattern.** All 60 winning markets entered below $0.85.
Top wins entered at $0.62 / $0.37 / $0.22 / $0.23, 10–15 days before resolution. Spans
Iran strikes, Russia-Ukraine ceasefire, China-Taiwan, and 2026 Winter Olympics medal markets.

## Caveat / honesty note

- Volume-weighted average prices look near $1.00 because these wallets cash out large positions
  at resolution; the *first-entry* price is the correct predictive metric and it is cheap.
- Both-sides trading rates (23–38%) are below the market-maker threshold, so these are
  directional bettors — unlike the earlier false positive 0x54b561… (a true MM, 3,937 trades).
- High accuracy on date-specific "will X happen by [date]" markets is partly structural: betting
  NO on a low-probability event is usually right. The fast-funding + cheap-entry + large-size
  combination is what elevates these above baseline.
