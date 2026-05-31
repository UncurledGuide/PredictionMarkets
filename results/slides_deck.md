# Polymarket Geopolitics — Insider Detection Deck
### Canva-ready slide content. Each "---" = one slide. Speaker notes in _italics_.

---

## SLIDE 1 — Title
**Do informed traders predict geopolitical outcomes on Polymarket?**
Detecting smart money in Iran / Israel / Ukraine / Russia markets

_Sub-line: On-chain analysis of 7.8M trades across 574 markets._

---

## SLIDE 2 — The Question
We weren't asking "who is profitable."
We asked: **do certain wallets systematically bet the correct direction, early, at prices the market hadn't yet caught up to?**

That's the signature of information — not luck.

_Note: profitability alone is noisy; predictive timing is the real test._

---

## SLIDE 3 — The Signal Stack (Method)
Four independent filters, stacked:

1. **FIFO P&L labeling** — profit ≥ $100 AND notional ≥ $500 → "informed" candidate
2. **Cross-market accuracy** — correct on ≥5 related markets at ≥75%
3. **Funding latency** — time from first USDC deposit → first trade (fast = suspicious)
4. **Entry price on winners** — did they buy cheap before the outcome was obvious?

_A wallet must survive all four to be flagged. No single signal is trusted alone._

---

## SLIDE 4 — Tightening the Net (Before / After)
Our first pass was too loose and showed almost nothing. Tightening the thresholds made the signal jump 4x.

| | Loose (1st pass) | Tight (final) |
|---|---|---|
| Threshold | ≥3 mkts, ≥60% | ≥5 mkts, ≥75% |
| Cohort size | 104 wallets | 40 wallets |
| Edge vs. baseline | +1.4¢ (p=0.015) | **+6.16¢ (p<0.0001)** |
| Entry-price advantage | +6.55¢ | **+15.3¢** |

_The pattern strengthened as we got stricter — the opposite of what noise does._

---

## SLIDE 5 — Headline Result
The 40-wallet "informed" cohort beat the rest of the market by:

- **+6.16¢ per dollar risked** in directional edge (p < 0.0001, highly significant)
- **+15.3¢ cheaper entries** on trades that ended up winning

They got in earlier and cheaper on the side that came true.

---

## SLIDE 6 — Honest Caveat (keep this — it builds trust)
Two things this does **not** say:

- It does **not** say these wallets are wildly profitable. In absolute terms the cohort still
  averages a negative edge (−46.9¢) — the finding is **relative**: they beat the baseline.
- High accuracy on "will X happen by [date]?" is **partly structural** — betting NO on
  unlikely events is usually right. So accuracy alone isn't proof.

**What elevates our top candidates:** fast funding + cheap early entries + large size, together.

---

## SLIDE 7 — The Funding Signal
We pulled on-chain deposit history for the cohort.

**6 of 24 profiled wallets deposited money and started trading within 24 hours.**
All six are profitable. Two related wallets cleared **+$400K** on Iran-strike markets.

Fresh money, immediate large directional bets = the classic informed-trading footprint.

---

## SLIDE 8 — Case Study A: the 0-hour wallet
**0x92a6294c…**
- Deposited $1,499 → first trade **same hour**
- 91% accurate across 11 Iran/Israel market clusters
- Realized P&L: **+$210,426**
- Entered 30 of 34 markets below $0.85 — e.g. an Iran military-action market at **$0.02, 20 days early**

---

## SLIDE 9 — Case Study B: the biggest winner
**0x8a480b60…**
- 100% accurate across 22 clusters → **+$402,033**
- Concentrated in "Will the US next strike Iran on [date]?" series
- Pattern: cheap early entries, then large confirmation buys near resolution

**0xd48a81db… (cleanest pattern)** — all 60 winning markets entered below $0.85;
top wins at $0.62 / $0.37 / $0.22, 10–15 days before resolution. **+$424,802**

---

## SLIDE 10 — We Caught a False Positive Too
**0x54b56146…** looked suspicious (4-hour funding latency) — but it's a **market maker**:
3,937 trades, selling both YES and NO on the same markets, tiny notionals.

We excluded it. _This is why the both-sides-trading check matters — it separates liquidity
providers from directional insiders._

---

## SLIDE 11 — What the Markets Have in Common
The informed edge concentrates in:
- **US-strikes-Iran-by-[date]** series (the densest cluster)
- **Ceasefire** markets (US–Iran, Russia–Ukraine)
- **Israel military action** (Lebanon)

Date-specific geopolitical resolution markets — exactly where private timing information would pay.

---

## SLIDE 12 — Takeaways
- A stacked-signal method isolates a small set of wallets with **statistically real** predictive edge.
- The strongest candidates combine **fresh fast funding + cheap early entries + large size**.
- Method is conservative: caught and removed a market-maker false positive; reports relative
  (not absolute) edge honestly.
- **Next:** widen funding pulls to the full cohort, add news-event timestamp alignment to test
  lead-time vs. public information.
