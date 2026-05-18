# Model Build Roadmap

**For:** the teammate building the classifier
**Workstream:** Model (spine — final stage)
**Assumes:** you've done gradient boosting before. This skips "what is LightGBM"
and focuses on the project-specific traps.

**Work with Claude in Cursor** for the actual code — paste this roadmap in, paste
errors in. This doc tells you *what* to build and *why the project-specific choices
matter*; Claude helps with the implementation.

---

## 1. What you're building

A classifier that scores each Polymarket trade by how likely it came from an
informed trader. Input: the `features` table. Target: the `label` column in
`trade_labels`. Output: a per-trade probability in [0, 1], plus a feature-importance
ranking — the ranking is half the actual research deliverable, not an afterthought.

You are NOT building a live system. This is an offline analysis on resolved markets.
That single fact drives most of the design decisions below.

---

## 1.5 — Step 0: generate the database first

You are NOT given a database. You get the **code** from the repo and generate
`data/trades.db` yourself by running the existing pipeline. Everyone on the team
regenerates data from code — that's how we keep it reproducible.

Clone the repo, install dependencies, then run these scripts **in this order**.
Each one builds a table the next depends on:

1. **Slug collector** (`market_collector.py` or equivalent) — pulls the resolved
   market catalog from Polymarket's Gamma API, and writes the `markets` table
   (with resolution outcomes) into `data/trades.db`.
2. **`trade_puller.py <condition_id>`** — pulls trade fills for the pilot market
   into the `trades` table. Pilot market condition id:
   `0x260fd9d6b10746909a26c2af7a68b409f757c95a07dc57ddd480774a36c8399b`
3. **`funding_puller.py`** — pulls USDC funding history for every wallet into
   `funding_events`. **Needs your own Etherscan API key** — free signup at
   `etherscan.io`, 2 minutes; the script reads it from a `.env` file as
   `ETHERSCAN_API_KEY`. This step takes ~30 min (it hits an external API).
4. **`build_wallet_features.py`** — aggregates funding into `wallet_features`.
5. **`build_features.py`** — builds the per-trade `features` table.
6. **`build_trade_labels.py`** — builds the `trade_labels` table (the y).

After all six run, `data/trades.db` contains every table the model needs. Two
things to know:

- Your DB will be the same *shape* as everyone else's but not byte-identical. The
  trade puller is capped by Polymarket's API at ~3,500 recent fills, and "recent"
  shifts over time — you may pull a slightly different slice. Fine for building and
  testing the model. (This cap is why the team is migrating to a subgraph; not your
  problem for v1.)
- If a script fails, it's idempotent — re-run it, it resumes. Don't rebuild from
  scratch.

Once `data/trades.db` exists with all six tables, everything below works as written.

---

## 2. The data — where it is and how to load it

After Step 0, everything is in one SQLite file: `data/trades.db`. You need two
tables, joined on `tx_hash`:

- **`features`** — one row per trade, the X. Columns are prefixed:
  - `exante_*` — computable from info available at/before the trade. Safe.
  - `post_*` — price-impact / persistence features that need a window *after* the
    trade. Useful but NOT strictly ex-ante. More on this in §6.
  - plus `tx_hash`, `condition_id`, `proxy_wallet`, `timestamp_unix` (keys/meta).
- **`trade_labels`** — one row per trade, the y. Use the `label` column (1 =
  informed, 0 = not). The table also has `wallet_market_pnl_usdc` (raw PnL) — keep
  it around, it lets you re-threshold or try regression later without re-running
  anything.

Load it like this:

```python
import sqlite3
import pandas as pd

conn = sqlite3.connect("data/trades.db")
df = pd.read_sql("""
    SELECT f.*, l.label, l.wallet_market_pnl_usdc, l.unmatched_sell_flag
    FROM features f
    JOIN trade_labels l ON f.tx_hash = l.tx_hash
""", conn)
conn.close()
```

That's the full modeling table. One row per trade, features + label.

**Do not** pull from the other tables (`trades`, `funding_events`, `wallet_features`)
— those are upstream pipeline stages. `features` already has everything aggregated.
If a feature you want is missing, that's a `build_features.py` change, not a model
change — raise it, don't reach around it.

---

## 3. The non-negotiable: time-series cross-validation

This is the trap that will silently ruin the project if you get it wrong.

**Do not use random K-fold.** Random K-fold puts trades from December in the
training set and trades from November in the validation set — you'd be training on
the future to predict the past. That leaks information, inflates your metrics, and
the model would look great while being worthless.

Use **`sklearn.model_selection.TimeSeriesSplit`**. Sort the data by
`timestamp_unix`, then TimeSeriesSplit gives you folds where the validation set is
always *later* than the training set. That mimics reality: a real predictor only
ever has the past.

```python
from sklearn.model_selection import TimeSeriesSplit

df = df.sort_values("timestamp_unix").reset_index(drop=True)
tscv = TimeSeriesSplit(n_splits=5)
for train_idx, val_idx in tscv.split(df):
    # train_idx is always earlier in time than val_idx
    ...
```

Every metric you report must come from this scheme. If you ever see a suspiciously
high score, the first thing to check is whether time ordering leaked.

---

## 4. The model

LightGBM, `LGBMClassifier`. Reasons it's the right pick here — tabular mixed-type
features, lots of missing values (see §6), automatic feature interactions, built-in
importance. You know boosting; the only project-specific notes:

- **Handle the class imbalance.** ~10.7% of trades are labeled informed. Use
  `class_weight="balanced"` or set `scale_pos_weight` to roughly (neg/pos). Don't
  skip this — without it the model can score 89% "accuracy" by predicting "not
  informed" for everything, which is useless.
- **Use early stopping.** Pass the validation fold as `eval_set` with
  `callbacks=[lightgbm.early_stopping(50)]` so it stops adding trees when validation
  performance plateaus. Prevents overfitting.
- **Sane starting hyperparameters** — `n_estimators=500`, `learning_rate=0.05`,
  `num_leaves=31`, `max_depth=6`, `min_child_samples=20`. Tune later with Optuna if
  there's time; do NOT tune for v1. Get a working baseline first.
- Fixed `random_state` everywhere for reproducibility.

---

## 5. Evaluation — accuracy is a trap, use these instead

With a 10.7% positive rate, **accuracy is meaningless** — predict all-zeros and
you're 89% accurate. Report these instead, averaged across the TimeSeriesSplit
folds:

- **ROC-AUC** (`roc_auc_score`) — probability the model ranks a random informed
  trade above a random non-informed one. 0.5 = useless, 1.0 = perfect. For this
  project 0.65-0.80 would be a genuinely good result. **Above ~0.90, be suspicious
  of leakage** — go re-check §3 and §6.
- **PR-AUC / average precision** (`average_precision_score`) — more honest than
  ROC-AUC when the positive class is rare. This is your primary metric.
- **Top-N precision** — of the N trades the model scores highest, how many were
  actually informed. This is the operationally meaningful one: a downstream system
  would flag the top-N most suspicious trades, so precision at the top of the
  ranking is what matters.

Report all three, per-fold and averaged. Never report accuracy alone.

---

## 6. The `exante_` vs `post_` decision — train it both ways

The `features` table has two kinds of columns:

- `exante_*` — strictly ex-ante. A real predictor would have these.
- `post_*` — price impact / persistence. They need a window *after* the trade, so
  they are NOT something a real-time system would have at decision time. They are
  included because persistence (did the price move stick) is theoretically one of
  the strongest informed-trade signals — but using them changes what the model
  *is*.

**Train two models and report both:**

1. **Ex-ante only** — drop all `post_*` columns. This is the honest predictive
   model: "given only what we knew at trade time, is this trade informed?"
2. **Ex-ante + post** — include everything. This is more of an *explanatory* model:
   "what trade characteristics, including aftermath, associate with informed trades?"

The comparison is itself a finding — how much does post-trade behavior add. Two
cautions on the `post_` features:

- They have **many nulls** (trades near the end of the data window have no
  post-window). That's expected. LightGBM handles missing values natively — don't
  impute, don't drop those rows.
- The leakage risk: `post_` features must not encode the *resolution* outcome. The
  windows are bounded (1h / 6h) specifically to limit this. If the ex-ante+post
  model scores wildly higher than ex-ante-only (e.g. AUC jumps from 0.72 to 0.95),
  treat that as a red flag for leakage, not a triumph.

Also drop these from the feature matrix before training — they're keys/metadata, not
features: `tx_hash`, `condition_id`, `proxy_wallet`, `timestamp_unix`, `computed_at`,
`wallet_market_pnl_usdc`, `unmatched_sell_flag`. (Keep `wallet_market_pnl_usdc`
aside — it's useful for analysis, but feeding it as a feature is direct label
leakage since the label is derived from it.)

---

## 7. Feature importance — this is half the deliverable

After training, pull `model.feature_importances_` and rank the features. This
answers the actual research question: *which signals separate informed trades from
noise?* Funding latency? Trade size? Volume impact? Persistence?

- Report the ranked importances for both the ex-ante and ex-ante+post models.
- If you have time, use **SHAP** values for a more rigorous per-feature attribution
  than the built-in importance — but the built-in ranking is fine for v1.
- Write down what the ranking *means*, not just the numbers. "Funding latency ranked
  #1, well above wallet size" is a sentence that goes in the SFIC writeup.

---

## 8. KNOWN LIMITATIONS — read this before interpreting anything

The pilot dataset is one market with real constraints. Do not over-claim what the
v1 model finds. Bake these into any results writeup:

- **Only 13 informed wallets.** The positive class is 374 trades but those come from
  just 13 distinct wallets. The model is effectively learning from 13 examples of
  "informed behavior." That is thin. v1 results are indicative, not conclusive.
- **Pilot data is the market's endgame.** The trade puller's API cap means we only
  have the last ~10 days of a months-long market. By that point the market had
  largely figured out the answer — so the "informed" label here mostly captures
  *traded the correct side during the obvious endgame*, not *knew something early*.
  Genuine foresight detection needs the full market history.
- **17.6% of trades have `unmatched_sell_flag`** — early buys were cut off by the
  cap, so some PnL is imprecise.
- **The label is a weak proxy.** "Made money" ≠ "was informed." Some 1s are luck,
  some 0s were informed-but-unlucky. This caps achievable AUC — a ceiling around
  0.75-0.80 may simply be the label noise, not a model failure.

All of these resolve when the subgraph migration delivers full history across ~138
markets. Which means:

**Build the harness to re-run cleanly on new data.** Don't hardcode anything to this
one market or these 3,500 rows. When the subgraph data lands, you should be able to
rerun the exact same script and get a real result. Parameterize the DB path, don't
assume row counts, don't assume one market.

---

## 9. Build order

1. [ ] **Step 0** — clone the repo, run the six pipeline scripts to generate
       `data/trades.db` (§1.5).
2. [ ] Load + join `features` and `trade_labels` into one DataFrame (§2).
3. [ ] Drop key/metadata/leakage columns; split into ex-ante-only and
       ex-ante+post feature sets (§6).
4. [ ] Set up `TimeSeriesSplit`, sorted by `timestamp_unix` (§3).
5. [ ] Train `LGBMClassifier` with class balancing + early stopping, per fold (§4).
6. [ ] Evaluate: ROC-AUC, PR-AUC, top-N precision, per-fold and averaged (§5).
7. [ ] Do it for both the ex-ante-only and ex-ante+post feature sets (§6).
8. [ ] Pull and rank feature importances for both (§7).
9. [ ] Write a short results summary — metrics + importance ranking + the §8
       caveats. This is the SFIC-facing output.

Deliver it as a script (`train_model.py`) plus a short results writeup. Make the
script re-runnable: `python train_model.py --db data/trades.db`.

---

## 10. Optional, only if v1 is done and solid

The current framing is "classify individual trades." A stronger research question
is "do informed-looking wallets *predict market outcomes* better than the market
price — and by how much, with what confidence?" That turns the model output into a
**smart-money signal** and backtests whether following it beats the market price.
It's a better, more verifiable result for SFIC — but it depends on the multi-market
subgraph data. Note it as a v2 direction; don't attempt it on the single-market
pilot.

---

## Definition of done

- [ ] Ran the six pipeline scripts; `data/trades.db` generated with all tables.
- [ ] `train_model.py` runs end-to-end from `data/trades.db`.
- [ ] Time-series CV, not random K-fold.
- [ ] ROC-AUC, PR-AUC, top-N precision reported per-fold and averaged.
- [ ] Both ex-ante-only and ex-ante+post models trained and compared.
- [ ] Feature-importance ranking produced for both.
- [ ] Results summary written, including the §8 limitations.
- [ ] Script re-runs cleanly on new data (no hardcoded market/row assumptions).

Stuck? Paste the step, your code, and the error into Claude.
