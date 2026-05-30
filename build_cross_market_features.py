"""Cross-market feature builder — fast SQL-first rewrite.

Produces `cross_market_features`: one row per trade with wallet accuracy
signals across correlated market clusters (geopolitics events).

Speed strategy (avoids the O(n²) Python-loop problem):
  1. SQL GROUP BY  — compute per-(wallet, market) outcome in the DB
  2. Python sort   — build per-wallet resolved-market timeline (sorted by resolve_ts)
  3. Binary search — for each trade, find accuracy at trade-time in O(log m)
  4. Chunked write — insert 50k rows at a time, never holding all 7.8M in RAM

Runtime: ~5–15 min for 7.8M trades.

Usage:
    python build_cross_market_features.py [--db data/trades.db] [--force]
"""

from __future__ import annotations

import argparse
import bisect
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("cross_market_features")

SCHEMA = """
DROP TABLE IF EXISTS cross_market_features;

CREATE TABLE cross_market_features (
    tx_hash                                 TEXT PRIMARY KEY,
    condition_id                            TEXT NOT NULL,
    proxy_wallet                            TEXT NOT NULL,
    timestamp_unix                          INTEGER NOT NULL,

    exante_cluster_id                       TEXT,
    exante_cluster_n_markets                INTEGER,
    exante_cluster_event_volume_rank        INTEGER,
    exante_market_age_days                  REAL,

    exante_wallet_cluster_n_prior           INTEGER,
    exante_wallet_cluster_correct_rate      REAL,
    exante_wallet_cluster_direction_consistent INTEGER,
    exante_wallet_cluster_avg_lead_days     REAL,

    exante_wallet_n_markets_total           INTEGER,
    exante_wallet_geo_correct_rate          REAL,

    computed_at                             TEXT NOT NULL
);

CREATE INDEX idx_cmf_wallet    ON cross_market_features(proxy_wallet);
CREATE INDEX idx_cmf_condition ON cross_market_features(condition_id);
CREATE INDEX idx_cmf_ts        ON cross_market_features(timestamp_unix);
CREATE INDEX idx_cmf_cluster   ON cross_market_features(exante_cluster_id);
"""

CHUNK_SIZE = 50_000


# ── cluster slug derivation ───────────────────────────────────────────────────

def _slug_to_cluster(market_slug: str) -> str:
    """Strip trailing date/deadline suffixes to group market variants."""
    slug = (market_slug or "").strip().lower()
    if not slug:
        return slug
    # Remove long Polymarket dedup tails (e.g. -227-191-...)
    slug = re.sub(r'(-\d{3,})+$', '', slug)
    # Remove trailing year
    slug = re.sub(r'-\d{4}$', '', slug)
    # Remove -by-<month>[-day], -before-<month>, -in-<month>
    months = (r'(?:january|february|march|april|may|june|july|august|'
              r'september|october|november|december)')
    slug = re.sub(rf'-(?:by|before|in|after)-{months}(?:-\d{{1,2}})?$', '', slug)
    # Remove -q[1-4]
    slug = re.sub(r'-q[1-4]$', '', slug)
    # Remove any remaining trailing bare numbers
    slug = re.sub(r'(-\d+)+$', '', slug)
    return slug or market_slug


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def build(db_path: Path, force: bool) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA cache_size=-262144")   # 256 MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row

    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for needed in ("trades", "markets"):
        if needed not in existing:
            raise RuntimeError(f"required table '{needed}' missing")

    if "cross_market_features" in existing and not force:
        n = conn.execute("SELECT COUNT(*) FROM cross_market_features").fetchone()[0]
        logger.warning("cross_market_features exists (%d rows). Use --force.", n)
        conn.close()
        return

    logger.info("rebuilding cross_market_features")
    conn.executescript(SCHEMA)
    computed_at = datetime.now(UTC).isoformat()

    # ── Phase 1: market metadata ──────────────────────────────────────────────
    markets_raw = [dict(r) for r in conn.execute(
        "SELECT condition_id, market_slug, volume_usd, resolution_outcome, end_date "
        "FROM markets"
    )]
    logger.info("loaded %d markets", len(markets_raw))

    # cluster assignments
    cid_to_cluster: dict[str, str] = {}
    cid_to_resolve_ts: dict[str, int | None] = {}
    cid_to_resolution: dict[str, str] = {}
    cid_to_volume: dict[str, float] = {}

    for m in markets_raw:
        cid = m["condition_id"]
        cid_to_cluster[cid] = _slug_to_cluster(m["market_slug"] or "")
        cid_to_resolve_ts[cid] = _parse_ts(m["end_date"])
        cid_to_resolution[cid] = (m["resolution_outcome"] or "").upper()
        cid_to_volume[cid] = float(m["volume_usd"] or 0)

    # cluster metadata: n_markets, volume rank per market
    cluster_markets: dict[str, list[str]] = defaultdict(list)
    for cid, cluster in cid_to_cluster.items():
        cluster_markets[cluster].append(cid)

    cid_to_cluster_size: dict[str, int] = {}
    cid_to_volume_rank: dict[str, int] = {}
    for cluster, cids in cluster_markets.items():
        cid_to_cluster_size.update({c: len(cids) for c in cids})
        sorted_cids = sorted(cids, key=lambda c: cid_to_volume.get(c, 0), reverse=True)
        for rank, c in enumerate(sorted_cids, 1):
            cid_to_volume_rank[c] = rank

    # ── Phase 2: per-(wallet, market) outcome via SQL GROUP BY ────────────────
    # Net YES notional: BUY YES = +, BUY NO = -, SELL YES = -, SELL NO = +
    logger.info("computing per-(wallet, market) outcomes via SQL...")
    wm_rows = conn.execute("""
        SELECT
            proxy_wallet,
            condition_id,
            MIN(timestamp_unix)  AS first_trade_ts,
            SUM(CASE
                WHEN UPPER(side)='BUY'  AND UPPER(outcome)='YES' THEN  notional_usdc
                WHEN UPPER(side)='BUY'  AND UPPER(outcome)='NO'  THEN -notional_usdc
                WHEN UPPER(side)='SELL' AND UPPER(outcome)='YES' THEN -notional_usdc
                WHEN UPPER(side)='SELL' AND UPPER(outcome)='NO'  THEN  notional_usdc
                ELSE 0 END)      AS net_yes_notional,
            COUNT(*)             AS n_trades
        FROM trades
        GROUP BY proxy_wallet, condition_id
    """).fetchall()
    logger.info("computed %d wallet-market pairs", len(wm_rows))

    # Build per-wallet history: list of resolved entries, sorted by resolve_ts
    # Each entry: (resolve_ts, cluster_id, correct: int, lead_days: float)
    # "correct" = 1 if wallet bet in winning direction

    wallet_cluster_history: dict[str, list] = defaultdict(list)
    wallet_all_history: dict[str, list] = defaultdict(list)  # for geo accuracy

    for row in wm_rows:
        wallet = row["proxy_wallet"]
        cid = row["condition_id"]
        resolve_ts = cid_to_resolve_ts.get(cid)
        resolution = cid_to_resolution.get(cid, "")
        cluster = cid_to_cluster.get(cid, "")
        first_trade_ts = row["first_trade_ts"]
        net_yes = row["net_yes_notional"] or 0.0

        if resolve_ts is None or resolution not in ("YES", "NO"):
            continue

        # Correctness: net YES position on YES market, or net NO on NO market
        correct = int(
            (net_yes > 0 and resolution == "YES") or
            (net_yes < 0 and resolution == "NO")
        )
        lead_days = (resolve_ts - first_trade_ts) / 86400.0 if first_trade_ts else None

        wallet_cluster_history[wallet].append((resolve_ts, cluster, cid, correct, lead_days))
        wallet_all_history[wallet].append((resolve_ts, cid, correct))

    # Sort each wallet's history by resolve_ts for binary search
    for wallet in wallet_cluster_history:
        wallet_cluster_history[wallet].sort(key=lambda x: x[0])
    for wallet in wallet_all_history:
        wallet_all_history[wallet].sort(key=lambda x: x[0])

    logger.info("built wallet history for %d wallets", len(wallet_cluster_history))

    # ── Phase 3: market open timestamps ──────────────────────────────────────
    # Use first trade per market as proxy for market open
    market_open_ts = dict(conn.execute(
        "SELECT condition_id, MIN(timestamp_unix) FROM trades GROUP BY condition_id"
    ).fetchall())

    # ── Phase 4: iterate trades, compute features, chunked insert ────────────
    logger.info("loading trades for feature computation...")
    n_total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    rows_buf = []
    inserted = 0

    # Process in chunks to avoid loading all 7.8M into memory at once
    offset = 0
    chunk = 200_000

    while True:
        trades_chunk = conn.execute(
            "SELECT tx_hash, condition_id, proxy_wallet, timestamp_unix "
            "FROM trades ORDER BY timestamp_unix ASC, tx_hash ASC "
            "LIMIT ? OFFSET ?", (chunk, offset)
        ).fetchall()
        if not trades_chunk:
            break

        for t in trades_chunk:
            tx = t["tx_hash"]
            cid = t["condition_id"]
            wallet = t["proxy_wallet"]
            ts = t["timestamp_unix"]

            cluster = cid_to_cluster.get(cid)
            cluster_size = cid_to_cluster_size.get(cid)
            vol_rank = cid_to_volume_rank.get(cid)
            open_ts = market_open_ts.get(cid)
            age_days = (ts - open_ts) / 86400.0 if open_ts else None

            # ── cluster accuracy at time ts ──
            cluster_n_prior = 0
            cluster_correct_rate = None
            cluster_avg_lead = None
            cluster_direction_consistent = None

            if cluster and wallet in wallet_cluster_history:
                hist = wallet_cluster_history[wallet]
                # Binary search: all entries with resolve_ts < ts AND cluster matches
                # hist is sorted by resolve_ts; find cutoff index
                cutoff = bisect.bisect_left(hist, (ts,))  # all entries < ts
                prior = [e for e in hist[:cutoff]
                         if e[1] == cluster and e[2] != cid]
                cluster_n_prior = len(prior)
                if prior:
                    cluster_correct_rate = sum(e[3] for e in prior) / len(prior)
                    leads = [e[4] for e in prior if e[4] is not None]
                    cluster_avg_lead = sum(leads) / len(leads) if leads else None
                    # direction consistent: all prior bets in cluster same direction?
                    # approximate: all correct (1) or all incorrect (0)
                    cluster_direction_consistent = int(
                        all(e[3] == prior[0][3] for e in prior)
                    ) if len(prior) > 1 else None

            # ── global geo accuracy at time ts ──
            geo_n_total = 0
            geo_correct_rate = None

            if wallet in wallet_all_history:
                all_hist = wallet_all_history[wallet]
                cutoff = bisect.bisect_left(all_hist, (ts,))
                prior_all = [e for e in all_hist[:cutoff] if e[1] != cid]
                geo_n_total = len(prior_all)
                if prior_all:
                    geo_correct_rate = sum(e[2] for e in prior_all) / len(prior_all)

            rows_buf.append((
                tx, cid, wallet, ts,
                cluster, cluster_size, vol_rank, age_days,
                cluster_n_prior,
                cluster_correct_rate,
                cluster_direction_consistent,
                cluster_avg_lead,
                geo_n_total,
                geo_correct_rate,
                computed_at,
            ))

        # Flush chunk to DB
        conn.executemany("""
            INSERT OR IGNORE INTO cross_market_features VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        """, rows_buf)
        conn.commit()
        inserted += len(rows_buf)
        rows_buf.clear()

        offset += chunk
        if offset % 500_000 == 0 or offset >= n_total:
            logger.info("  progress: %d/%d trades (%.1f%%)",
                        min(offset, n_total), n_total,
                        100 * min(offset, n_total) / n_total)

    logger.info("done — inserted %d rows", inserted)
    print_summary(conn)
    conn.close()


def print_summary(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM cross_market_features").fetchone()[0]
    n_with_prior = cur.execute(
        "SELECT COUNT(*) FROM cross_market_features WHERE exante_wallet_cluster_n_prior > 0"
    ).fetchone()[0]
    n_strong = cur.execute(
        "SELECT COUNT(*) FROM cross_market_features "
        "WHERE exante_wallet_cluster_n_prior >= 3 "
        "  AND exante_wallet_cluster_correct_rate >= 0.67"
    ).fetchone()[0]

    print("\n=== cross_market_features summary ===")
    print(f"  total rows:                   {total:,}")
    print(f"  rows with cluster history:    {n_with_prior:,}")
    print(f"  strong signal rows            {n_strong:,}  "
          f"(>=3 prior, correct_rate>=0.67)")

    print("\n  top clusters by informed-trade count:")
    rows = cur.execute("""
        SELECT exante_cluster_id, COUNT(*) as n
        FROM cross_market_features
        WHERE exante_wallet_cluster_n_prior >= 3
          AND exante_wallet_cluster_correct_rate >= 0.67
        GROUP BY exante_cluster_id
        ORDER BY n DESC
        LIMIT 10
    """).fetchall()
    for cluster, n in rows:
        print(f"    {cluster:<45}  {n:>7,}")


def main() -> int:
    parser = argparse.ArgumentParser()
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
