"""Trade-level feature extraction (Stage 2).

Produces the `features` table: one row per trade, joining wallet-level
features and computing trade-specific features. This is the main input
to the model (after the labeling team's `trade_labels` table is joined on).

Columns are grouped by prefix:
  exante_*  - computable using only info available at/before the trade.
  post_*    - require a post-trade window (price impact / persistence).
              Bounded to 1h and 6h, drift-normalized to limit leakage.

Usage:
    python build_features.py [--db data/trades.db] [--force]

Pure SQL + Python aggregation, no API calls. Depends on:
    trades, wallet_features  (run trade_puller, funding_puller,
                              build_wallet_features first)
"""

from __future__ import annotations

import argparse
import bisect
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("build_features")

SECONDS_24H = 86_400
SECONDS_1H = 3_600
SECONDS_6H = 21_600

SCHEMA = """
DROP TABLE IF EXISTS features;

CREATE TABLE features (
    tx_hash                         TEXT PRIMARY KEY,
    condition_id                    TEXT NOT NULL,
    proxy_wallet                    TEXT NOT NULL,
    timestamp_unix                  INTEGER NOT NULL,

    exante_side                     TEXT NOT NULL,
    exante_outcome                  TEXT,
    exante_size                     REAL NOT NULL,
    exante_price                    REAL NOT NULL,
    exante_notional_usdc            REAL NOT NULL,
    exante_trailing_24h_volume      REAL NOT NULL,
    exante_volume_impact            REAL,
    exante_trailing_24h_n_trades    INTEGER NOT NULL,
    exante_wallet_n_prior_trades    INTEGER NOT NULL,
    exante_wallet_trade_seq         INTEGER NOT NULL,
    exante_secs_since_wallet_first  INTEGER NOT NULL,
    exante_hour_of_day              INTEGER NOT NULL,

    exante_wallet_funding_latency_s INTEGER,
    exante_wallet_first_fund_amt    REAL,
    exante_wallet_total_in_usdc     REAL,
    exante_wallet_net_usdc          REAL,
    exante_wallet_n_trades_total    INTEGER,
    exante_wallet_total_notional    REAL,
    exante_wallet_fund_source_type  TEXT,
    exante_wallet_funding_capped    INTEGER,

    post_price_1h_after             REAL,
    post_price_6h_after             REAL,
    post_price_move_1h              REAL,
    post_price_move_6h              REAL,
    post_jump_at_trade_1h           REAL,
    post_jump_at_trade_6h           REAL,
    post_retention_1h               REAL,
    post_retention_6h               REAL,
    post_window_coverage_1h         INTEGER NOT NULL,
    post_window_coverage_6h         INTEGER NOT NULL,

    computed_at                     TEXT NOT NULL
);

CREATE INDEX idx_feat_wallet    ON features(proxy_wallet);
CREATE INDEX idx_feat_condition ON features(condition_id);
CREATE INDEX idx_feat_ts        ON features(timestamp_unix);
"""


def orient_price_to_direction(price: float, side: str, outcome: str) -> float:
    """Convert execution price into a direction-the-trade-is-betting value."""
    if outcome is not None and outcome.lower() == "no":
        p = 1.0 - price
    else:
        p = price
    if side.upper() == "SELL":
        p = 1.0 - p
    return p


def _post_window(trades, ts, i, now, window_secs):
    """Compute post-trade price persistence over `window_secs` after trade i."""
    end = now + window_secs
    lo = i + 1
    hi = bisect.bisect_right(ts, end)
    win = trades[lo:hi]
    coverage = len(win)
    if coverage == 0:
        return None, None, None, None, 0

    this_oriented = trades[i]["_oriented"]
    price_after = win[-1]["_oriented"]
    price_move = price_after - this_oriented

    if i > 0:
        pre_oriented = trades[i - 1]["_oriented"]
        jump_at_trade = this_oriented - pre_oriented
        retention = price_after - pre_oriented
    else:
        jump_at_trade = 0.0
        retention = price_move

    return price_after, price_move, jump_at_trade, retention, coverage


def build(db_path: Path, force: bool) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for needed in ("trades", "wallet_features"):
        if needed not in existing:
            raise RuntimeError(f"required table '{needed}' missing — run prior stages first")

    if "features" in existing and not force:
        n = conn.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        logger.warning("features already exists (%d rows). Use --force to rebuild.", n)
        conn.close()
        return

    logger.info("rebuilding features table")
    conn.executescript(SCHEMA)
    computed_at = datetime.now(UTC).isoformat()

    wf = {}
    for r in conn.execute("SELECT * FROM wallet_features"):
        wf[r["proxy_wallet"]] = r
    logger.info("loaded %d wallet_features rows", len(wf))

    trades = [dict(r) for r in conn.execute(
        "SELECT * FROM trades ORDER BY timestamp_unix ASC, tx_hash ASC"
    )]
    n = len(trades)
    logger.info("loaded %d trades", n)
    if n == 0:
        logger.warning("no trades — nothing to do")
        conn.close()
        return

    for t in trades:
        t["_oriented"] = orient_price_to_direction(t["price"], t["side"], t["outcome"])

    ts = [t["timestamp_unix"] for t in trades]

    wallet_seen: dict[str, int] = {}
    wallet_first_ts: dict[str, int] = {}
    for t in trades:
        w = t["proxy_wallet"]
        if w not in wallet_first_ts:
            wallet_first_ts[w] = t["timestamp_unix"]

    rows = []
    for i, t in enumerate(trades):
        now = t["timestamp_unix"]
        w = t["proxy_wallet"]

        lo = bisect.bisect_left(ts, now - SECONDS_24H)
        hi = bisect.bisect_left(ts, now)
        window = trades[lo:hi]
        trailing_vol = sum(x["notional_usdc"] for x in window)
        trailing_n = len(window)
        vol_impact = (t["notional_usdc"] / trailing_vol) if trailing_vol > 0 else None

        seq = wallet_seen.get(w, 0) + 1
        wallet_seen[w] = seq
        n_prior = seq - 1
        secs_since_first = now - wallet_first_ts[w]

        p1, mv1, jump1, ret1, cov1 = _post_window(trades, ts, i, now, SECONDS_1H)
        p6, mv6, jump6, ret6, cov6 = _post_window(trades, ts, i, now, SECONDS_6H)

        wfr = wf.get(w)

        rows.append({
            "tx_hash": t["tx_hash"],
            "condition_id": t["condition_id"],
            "proxy_wallet": w,
            "timestamp_unix": now,

            "exante_side": t["side"],
            "exante_outcome": t["outcome"],
            "exante_size": t["size"],
            "exante_price": t["price"],
            "exante_notional_usdc": t["notional_usdc"],
            "exante_trailing_24h_volume": trailing_vol,
            "exante_volume_impact": vol_impact,
            "exante_trailing_24h_n_trades": trailing_n,
            "exante_wallet_n_prior_trades": n_prior,
            "exante_wallet_trade_seq": seq,
            "exante_secs_since_wallet_first": secs_since_first,
            "exante_hour_of_day": (now // SECONDS_1H) % 24,

            "exante_wallet_funding_latency_s": wfr["funding_to_first_trade_seconds"] if wfr else None,
            "exante_wallet_first_fund_amt": wfr["first_funding_amount_usdc"] if wfr else None,
            "exante_wallet_total_in_usdc": wfr["total_in_usdc"] if wfr else None,
            "exante_wallet_net_usdc": wfr["net_usdc"] if wfr else None,
            "exante_wallet_n_trades_total": wfr["n_trades"] if wfr else None,
            "exante_wallet_total_notional": wfr["total_notional_usdc"] if wfr else None,
            "exante_wallet_fund_source_type": wfr["first_funding_source_type"] if wfr else None,
            "exante_wallet_funding_capped": wfr["funding_capped"] if wfr else None,

            "post_price_1h_after": p1,
            "post_price_6h_after": p6,
            "post_price_move_1h": mv1,
            "post_price_move_6h": mv6,
            "post_jump_at_trade_1h": jump1,
            "post_jump_at_trade_6h": jump6,
            "post_retention_1h": ret1,
            "post_retention_6h": ret6,
            "post_window_coverage_1h": cov1,
            "post_window_coverage_6h": cov6,

            "computed_at": computed_at,
        })

    conn.executemany("""
        INSERT INTO features VALUES (
            :tx_hash, :condition_id, :proxy_wallet, :timestamp_unix,
            :exante_side, :exante_outcome, :exante_size, :exante_price,
            :exante_notional_usdc, :exante_trailing_24h_volume, :exante_volume_impact,
            :exante_trailing_24h_n_trades, :exante_wallet_n_prior_trades,
            :exante_wallet_trade_seq, :exante_secs_since_wallet_first, :exante_hour_of_day,
            :exante_wallet_funding_latency_s, :exante_wallet_first_fund_amt,
            :exante_wallet_total_in_usdc, :exante_wallet_net_usdc,
            :exante_wallet_n_trades_total, :exante_wallet_total_notional,
            :exante_wallet_fund_source_type, :exante_wallet_funding_capped,
            :post_price_1h_after, :post_price_6h_after,
            :post_price_move_1h, :post_price_move_6h,
            :post_jump_at_trade_1h, :post_jump_at_trade_6h,
            :post_retention_1h, :post_retention_6h,
            :post_window_coverage_1h, :post_window_coverage_6h,
            :computed_at
        )
    """, rows)
    conn.commit()

    print_summary(conn)
    conn.close()
    logger.info("done")


def print_summary(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    no_volimpact = cur.execute(
        "SELECT COUNT(*) FROM features WHERE exante_volume_impact IS NULL"
    ).fetchone()[0]
    no_1h = cur.execute(
        "SELECT COUNT(*) FROM features WHERE post_window_coverage_1h = 0"
    ).fetchone()[0]
    no_6h = cur.execute(
        "SELECT COUNT(*) FROM features WHERE post_window_coverage_6h = 0"
    ).fetchone()[0]
    no_wf = cur.execute(
        "SELECT COUNT(*) FROM features WHERE exante_wallet_funding_latency_s IS NULL"
    ).fetchone()[0]
    first_trades = cur.execute(
        "SELECT COUNT(*) FROM features WHERE exante_wallet_trade_seq = 1"
    ).fetchone()[0]

    print("\n=== features summary ===")
    print(f"  total trades:                 {total}")
    print(f"  wallet's-first-trade rows:    {first_trades}")
    print(f"  null volume_impact:           {no_volimpact}  (no trades in prior 24h)")
    print(f"  null 1h persistence:          {no_1h}  (no post-window data)")
    print(f"  null 6h persistence:          {no_6h}")
    print(f"  null wallet funding latency:  {no_wf}  (wallet not in wallet_features)")

    vi = cur.execute("""
        SELECT exante_volume_impact FROM features
        WHERE exante_volume_impact IS NOT NULL
        ORDER BY exante_volume_impact
    """).fetchall()
    if vi:
        v = [r[0] for r in vi]
        def pct(p): return v[min(int(p / 100 * len(v)), len(v) - 1)]
        print("\n  volume_impact distribution:")
        print(f"    p50={pct(50):.4f}  p90={pct(90):.4f}  p99={pct(99):.4f}  max={v[-1]:.4f}")

    print("\n  highest 6h retention trades (persistence signal candidates):")
    rows = cur.execute("""
        SELECT proxy_wallet, exante_notional_usdc, post_retention_6h,
               post_jump_at_trade_6h, post_window_coverage_6h
        FROM features
        WHERE post_retention_6h IS NOT NULL
        ORDER BY post_retention_6h DESC
        LIMIT 10
    """).fetchall()
    for w, notional, ret, jump, cov in rows:
        print(f"    {w[:14]}...  ${notional:>11,.0f}  retention_6h={ret:+.4f}  "
              f"jump={jump:+.4f}  cov={cov}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the trade-level features table.")
    parser.add_argument("--db", default="data/trades.db")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    build(Path(args.db), args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
