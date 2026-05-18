"""FIFO-PnL trade labeler — builds the `trade_labels` table.

This is the answer key for the model. For each (wallet, market) it computes
realized + resolution-settled PnL using FIFO accounting, thresholds that into
a binary informed/not label, and broadcasts the label onto every trade the
wallet made on that market (keyed by tx_hash so it joins to `features`).

Pipeline position:
    features      = X  (what each trade looked like)
    trade_labels  = y  (was each trade informed)   <-- this script
    model trains on features JOIN trade_labels ON tx_hash

Method:
  - FIFO is run independently per (wallet, market, outcome-token).
  - Realized PnL: each SELL is matched against the wallet's oldest unmatched
    BUYs of the same token; profit = (sell_price - buy_price) * shares.
  - Resolution PnL: shares still held at market close are settled at $1 if
    that token won, $0 if it lost.
  - total_pnl = realized + resolution, summed across both tokens.
  - Label = 1 (informed) iff total_pnl >= PNL_THRESHOLD AND the wallet's
    total traded notional on the market >= NOTIONAL_THRESHOLD. Else 0.
  - Raw pnl_usdc is kept alongside the binary label.

Caveat (state this in the writeup): "made money" is a noisy PROXY for
"informed". An informed trader can lose; a lucky one can win. The label is a
weak label and caps achievable model performance. Thresholding reduces, does
not remove, this noise. Better labels (risk-adjusted, cross-market
consistency) need the multi-market dataset and are future work.

Usage:
    python build_trade_labels.py [--db data/trades.db]
                                 [--pnl-threshold 100] [--notional-threshold 500]
                                 [--force]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("trade_labels")

DEFAULT_PNL_THRESHOLD = 100.0
DEFAULT_NOTIONAL_THRESHOLD = 500.0

SCHEMA = """
DROP TABLE IF EXISTS trade_labels;

CREATE TABLE trade_labels (
    tx_hash                 TEXT PRIMARY KEY,
    condition_id            TEXT NOT NULL,
    proxy_wallet            TEXT NOT NULL,
    wallet_market_pnl_usdc  REAL NOT NULL,
    wallet_market_realized  REAL NOT NULL,
    wallet_market_resolution REAL NOT NULL,
    wallet_market_notional  REAL NOT NULL,
    label                   INTEGER NOT NULL,
    pnl_threshold           REAL NOT NULL,
    notional_threshold      REAL NOT NULL,
    unmatched_sell_flag     INTEGER NOT NULL,
    computed_at             TEXT NOT NULL
);

CREATE INDEX idx_tl_wallet    ON trade_labels(proxy_wallet);
CREATE INDEX idx_tl_condition ON trade_labels(condition_id);
CREATE INDEX idx_tl_label     ON trade_labels(label);
"""


def fifo_realized_pnl(trades_for_token: list[dict]) -> tuple[float, float, deque, bool]:
    """Run FIFO matching over one wallet's trades on ONE outcome token."""
    open_lots: deque = deque()
    realized = 0.0
    notional = 0.0
    unmatched_sell = False

    for t in trades_for_token:
        side = (t["side"] or "").upper()
        shares = float(t["size"])
        price = float(t["price"])
        notional += shares * price

        if side == "BUY":
            open_lots.append([shares, price])
        elif side == "SELL":
            remaining = shares
            while remaining > 1e-9 and open_lots:
                lot = open_lots[0]
                matched = min(remaining, lot[0])
                realized += (price - lot[1]) * matched
                lot[0] -= matched
                remaining -= matched
                if lot[0] <= 1e-9:
                    open_lots.popleft()
            if remaining > 1e-9:
                unmatched_sell = True

    return realized, notional, open_lots, unmatched_sell


def settle_holdings(open_lots: deque, token_won: bool) -> float:
    payout = 1.0 if token_won else 0.0
    pnl = 0.0
    for shares, buy_price in open_lots:
        pnl += (payout - buy_price) * shares
    return pnl


def build(
    db_path: Path,
    pnl_threshold: float,
    notional_threshold: float,
    force: bool,
) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for needed in ("trades", "markets"):
        if needed not in existing:
            raise RuntimeError(
                f"required table '{needed}' missing — "
                f"run market_collector.py and trade_puller.py first"
            )
    if "trade_labels" in existing and not force:
        n = conn.execute("SELECT COUNT(*) FROM trade_labels").fetchone()[0]
        logger.warning("trade_labels exists (%d rows). Use --force to rebuild.", n)
        conn.close()
        return

    resolution = {
        r["condition_id"]: (r["resolution_outcome"] or "").upper()
        for r in conn.execute("SELECT condition_id, resolution_outcome FROM markets")
    }

    trades = [dict(r) for r in conn.execute(
        "SELECT tx_hash, condition_id, proxy_wallet, side, outcome, size, price, "
        "timestamp_unix FROM trades ORDER BY timestamp_unix ASC, tx_hash ASC"
    )]
    logger.info("loaded %d trades", len(trades))
    if not trades:
        logger.warning("no trades — nothing to label")
        conn.close()
        return

    groups: dict[tuple[str, str], list[dict]] = {}
    for t in trades:
        groups.setdefault((t["proxy_wallet"], t["condition_id"]), []).append(t)

    computed_at = datetime.now(UTC).isoformat()
    out_rows = []
    skipped_no_resolution = set()

    for (wallet, market), wt in groups.items():
        outcome_res = resolution.get(market)
        if outcome_res not in ("YES", "NO"):
            skipped_no_resolution.add(market)
            continue

        by_token: dict[str, list[dict]] = {}
        for t in wt:
            tok = (t["outcome"] or "").upper()
            by_token.setdefault(tok, []).append(t)

        total_realized = 0.0
        total_resolution = 0.0
        total_notional = 0.0
        any_unmatched = False

        for tok, token_trades in by_token.items():
            realized, notional, open_lots, unmatched = fifo_realized_pnl(token_trades)
            token_won = (tok == outcome_res)
            res_pnl = settle_holdings(open_lots, token_won)

            total_realized += realized
            total_resolution += res_pnl
            total_notional += notional
            any_unmatched = any_unmatched or unmatched

        total_pnl = total_realized + total_resolution
        label = int(
            total_pnl >= pnl_threshold and total_notional >= notional_threshold
        )

        for t in wt:
            out_rows.append({
                "tx_hash": t["tx_hash"],
                "condition_id": market,
                "proxy_wallet": wallet,
                "wallet_market_pnl_usdc": total_pnl,
                "wallet_market_realized": total_realized,
                "wallet_market_resolution": total_resolution,
                "wallet_market_notional": total_notional,
                "label": label,
                "pnl_threshold": pnl_threshold,
                "notional_threshold": notional_threshold,
                "unmatched_sell_flag": int(any_unmatched),
                "computed_at": computed_at,
            })

    conn.executescript(SCHEMA)
    conn.executemany("""
        INSERT OR IGNORE INTO trade_labels VALUES (
            :tx_hash, :condition_id, :proxy_wallet,
            :wallet_market_pnl_usdc, :wallet_market_realized, :wallet_market_resolution,
            :wallet_market_notional, :label, :pnl_threshold, :notional_threshold,
            :unmatched_sell_flag, :computed_at
        )
    """, out_rows)
    conn.commit()

    print_summary(conn, skipped_no_resolution, pnl_threshold, notional_threshold)
    conn.close()
    logger.info("done")


def print_summary(conn, skipped_markets, pnl_thr, notional_thr) -> None:
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM trade_labels").fetchone()[0]
    n_pos = cur.execute("SELECT COUNT(*) FROM trade_labels WHERE label=1").fetchone()[0]
    n_neg = total - n_pos
    n_wallets_pos = cur.execute(
        "SELECT COUNT(DISTINCT proxy_wallet) FROM trade_labels WHERE label=1"
    ).fetchone()[0]
    n_wallets_tot = cur.execute(
        "SELECT COUNT(DISTINCT proxy_wallet) FROM trade_labels"
    ).fetchone()[0]
    n_unmatched = cur.execute(
        "SELECT COUNT(*) FROM trade_labels WHERE unmatched_sell_flag=1"
    ).fetchone()[0]

    print("\n=== trade_labels summary ===")
    print(f"  thresholds:  pnl >= ${pnl_thr:,.0f}  AND  notional >= ${notional_thr:,.0f}")
    print(f"  total trades labeled:         {total}")
    if total:
        print(f"  label=1 (informed):           {n_pos}  ({100*n_pos/total:.1f}%)")
        print(f"  label=0 (not):                {n_neg}  ({100*n_neg/total:.1f}%)")
    print(f"  distinct wallets:             {n_wallets_tot}")
    print(f"  distinct informed wallets:    {n_wallets_pos}")
    print(f"  trades w/ unmatched-sell flag:{n_unmatched}")

    if skipped_markets:
        print(f"\n  WARNING: {len(skipped_markets)} market(s) had no resolution outcome —")
        print(f"  their trades were NOT labeled. Check the markets table.")

    seen = {}
    for r in cur.execute(
        "SELECT proxy_wallet, condition_id, wallet_market_pnl_usdc FROM trade_labels"
    ):
        seen[(r[0], r[1])] = r[2]
    vals = sorted(seen.values())
    if vals:
        def pct(p):
            return vals[min(int(p / 100 * len(vals)), len(vals) - 1)]

        print("\n  per-(wallet,market) PnL distribution:")
        print(f"    min={vals[0]:,.0f}  p25={pct(25):,.0f}  p50={pct(50):,.0f}  "
              f"p75={pct(75):,.0f}  p95={pct(95):,.0f}  max={vals[-1]:,.0f}")

    print("\n  top 10 (wallet,market) by PnL:")
    rows = cur.execute("""
        SELECT proxy_wallet, wallet_market_pnl_usdc, wallet_market_notional, label
        FROM (
            SELECT DISTINCT proxy_wallet, condition_id,
                   wallet_market_pnl_usdc, wallet_market_notional, label
            FROM trade_labels
        )
        ORDER BY wallet_market_pnl_usdc DESC
        LIMIT 10
    """).fetchall()
    for w, pnl, notional, lab in rows:
        print(f"    {w[:14]}...  pnl=${pnl:>12,.0f}  notional=${notional:>12,.0f}  "
              f"label={lab}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the trade_labels table (FIFO-PnL).")
    parser.add_argument("--db", default="data/trades.db")
    parser.add_argument("--pnl-threshold", type=float, default=DEFAULT_PNL_THRESHOLD)
    parser.add_argument("--notional-threshold", type=float, default=DEFAULT_NOTIONAL_THRESHOLD)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build(Path(args.db), args.pnl_threshold, args.notional_threshold, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
