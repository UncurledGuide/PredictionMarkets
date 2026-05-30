"""Wallet feature aggregator.

Collapses the raw funding_events table (one row per USDC transfer) and the
trades table into a wallet_features table: one row per proxy wallet, with
derived funding/trading features.

The headline feature is funding_to_first_trade_seconds — the gap between a
wallet's first real (non-infrastructure) USDC funding event and its first
trade. This is the legitimate replacement for the original detector's broken
nonce-based "freshness" signal.

Usage:
    python build_wallet_features.py [--db data/trades.db] [--force]

Pure SQL aggregation — no API calls. Runs in seconds.
Idempotent: rebuilds the wallet_features table from scratch each run.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("wallet_features")

# Polymarket infrastructure addresses. USDC moving to/from these is trade
# settlement, NOT funding. Excluded when computing "first funding event".
# Lowercase. Expand this set as more infra contracts are identified.
POLYMARKET_INFRA = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",  # CTF Exchange
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",  # Neg-risk / CLOB infra
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",  # Conditional Tokens framework
    "0x0000000000000000000000000000000000000000",  # zero address (mints/burns)
}

SCHEMA = """
DROP TABLE IF EXISTS wallet_features;

CREATE TABLE wallet_features (
    proxy_wallet                    TEXT PRIMARY KEY,
    -- trade-side aggregates
    n_trades                        INTEGER NOT NULL,
    total_notional_usdc             REAL NOT NULL,
    first_trade_unix                INTEGER,
    last_trade_unix                 INTEGER,
    -- funding-side aggregates
    n_in_events                     INTEGER NOT NULL,
    n_out_events                    INTEGER NOT NULL,
    total_in_usdc                   REAL NOT NULL,
    total_out_usdc                  REAL NOT NULL,
    net_usdc                        REAL NOT NULL,
    -- first real funding (infra excluded)
    first_funding_unix              INTEGER,
    first_funding_counterparty      TEXT,
    first_funding_amount_usdc       REAL,
    first_funding_source_type       TEXT,
    -- headline derived feature
    funding_to_first_trade_seconds  INTEGER,
    -- counterparty graph
    n_distinct_in_counterparties    INTEGER NOT NULL,
    n_proxy_in_counterparties       INTEGER NOT NULL,
    in_volume_from_proxies_usdc     REAL NOT NULL,
    fraction_in_from_proxies        REAL NOT NULL,
    -- data quality
    funding_capped                  INTEGER NOT NULL,
    computed_at                     TEXT NOT NULL
);

CREATE INDEX idx_wf_source_type ON wallet_features(first_funding_source_type);
CREATE INDEX idx_wf_latency     ON wallet_features(funding_to_first_trade_seconds);
"""


def build(db_path: Path, force: bool) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")  # wait up to 60s for write lock

    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for needed in ("trades", "funding_events"):
        if needed not in existing:
            raise RuntimeError(f"required table '{needed}' missing — run the pullers first")

    if "wallet_features" in existing and not force:
        n = conn.execute("SELECT COUNT(*) FROM wallet_features").fetchone()[0]
        logger.warning(
            "wallet_features already exists (%d rows). Use --force to rebuild.", n
        )
        conn.close()
        return

    logger.info("rebuilding wallet_features table")
    conn.executescript(SCHEMA)

    infra_list = ",".join(f"'{a}'" for a in POLYMARKET_INFRA)
    computed_at = datetime.now(UTC).isoformat()

    conn.execute("""
        CREATE TEMP TABLE known_proxies AS
        SELECT DISTINCT proxy_wallet AS addr FROM trades;
    """)

    conn.execute("""
        CREATE TEMP TABLE t_trade AS
        SELECT
            proxy_wallet,
            COUNT(*)                  AS n_trades,
            SUM(notional_usdc)        AS total_notional_usdc,
            MIN(timestamp_unix)       AS first_trade_unix,
            MAX(timestamp_unix)       AS last_trade_unix
        FROM trades
        GROUP BY proxy_wallet;
    """)

    conn.execute("""
        CREATE TEMP TABLE t_fund AS
        SELECT
            proxy_wallet,
            SUM(direction = 'IN')                                  AS n_in_events,
            SUM(direction = 'OUT')                                 AS n_out_events,
            COALESCE(SUM(CASE WHEN direction='IN'  THEN amount_usdc END), 0) AS total_in_usdc,
            COALESCE(SUM(CASE WHEN direction='OUT' THEN amount_usdc END), 0) AS total_out_usdc
        FROM funding_events
        GROUP BY proxy_wallet;
    """)

    conn.execute(f"""
        CREATE TEMP TABLE t_firstfund AS
        SELECT
            proxy_wallet,
            timestamp_unix    AS first_funding_unix,
            counterparty      AS first_funding_counterparty,
            amount_usdc       AS first_funding_amount_usdc,
            first_funding_source_type
        FROM (
            SELECT
                fe.proxy_wallet,
                fe.timestamp_unix,
                fe.counterparty,
                fe.amount_usdc,
                CASE WHEN kp.addr IS NOT NULL THEN 'polymarket_proxy' ELSE 'external' END
                    AS first_funding_source_type,
                ROW_NUMBER() OVER (
                    PARTITION BY fe.proxy_wallet
                    ORDER BY fe.timestamp_unix ASC, fe.amount_usdc DESC
                ) AS rn
            FROM funding_events fe
            LEFT JOIN known_proxies kp ON fe.counterparty = kp.addr
            WHERE fe.direction = 'IN'
              AND fe.counterparty NOT IN ({infra_list})
        )
        WHERE rn = 1;
    """)

    conn.execute(f"""
        CREATE TEMP TABLE t_graph AS
        SELECT
            fe.proxy_wallet,
            COUNT(DISTINCT fe.counterparty)                           AS n_distinct_in_counterparties,
            COUNT(DISTINCT CASE WHEN kp.addr IS NOT NULL
                                THEN fe.counterparty END)             AS n_proxy_in_counterparties,
            COALESCE(SUM(CASE WHEN kp.addr IS NOT NULL
                              THEN fe.amount_usdc END), 0)            AS in_volume_from_proxies_usdc
        FROM funding_events fe
        LEFT JOIN known_proxies kp ON fe.counterparty = kp.addr
        WHERE fe.direction = 'IN'
          AND fe.counterparty NOT IN ({infra_list})
        GROUP BY fe.proxy_wallet;
    """)

    conn.execute("""
        CREATE TEMP TABLE t_capped AS
        SELECT proxy_wallet, 1 AS funding_capped
        FROM funding_events
        GROUP BY proxy_wallet
        HAVING COUNT(*) >= 10000;
    """)

    conn.execute(f"""
        INSERT INTO wallet_features
        SELECT
            tt.proxy_wallet,
            tt.n_trades,
            tt.total_notional_usdc,
            tt.first_trade_unix,
            tt.last_trade_unix,
            COALESCE(tf.n_in_events, 0),
            COALESCE(tf.n_out_events, 0),
            COALESCE(tf.total_in_usdc, 0),
            COALESCE(tf.total_out_usdc, 0),
            COALESCE(tf.total_in_usdc, 0) - COALESCE(tf.total_out_usdc, 0),
            ff.first_funding_unix,
            ff.first_funding_counterparty,
            ff.first_funding_amount_usdc,
            ff.first_funding_source_type,
            CASE WHEN ff.first_funding_unix IS NOT NULL
                 THEN tt.first_trade_unix - ff.first_funding_unix
                 ELSE NULL END,
            COALESCE(tg.n_distinct_in_counterparties, 0),
            COALESCE(tg.n_proxy_in_counterparties, 0),
            COALESCE(tg.in_volume_from_proxies_usdc, 0),
            CASE WHEN COALESCE(tf.total_in_usdc, 0) > 0
                 THEN COALESCE(tg.in_volume_from_proxies_usdc, 0) / tf.total_in_usdc
                 ELSE 0 END,
            COALESCE(tc.funding_capped, 0),
            '{computed_at}'
        FROM t_trade tt
        LEFT JOIN t_fund      tf ON tt.proxy_wallet = tf.proxy_wallet
        LEFT JOIN t_firstfund ff ON tt.proxy_wallet = ff.proxy_wallet
        LEFT JOIN t_graph     tg ON tt.proxy_wallet = tg.proxy_wallet
        LEFT JOIN t_capped    tc ON tt.proxy_wallet = tc.proxy_wallet;
    """)
    conn.commit()

    print_summary(conn)
    conn.close()
    logger.info("done")


def print_summary(conn: sqlite3.Connection) -> None:
    """Print sanity-check stats so the output can be eyeballed immediately."""
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM wallet_features").fetchone()[0]

    no_funding = cur.execute(
        "SELECT COUNT(*) FROM wallet_features WHERE first_funding_unix IS NULL"
    ).fetchone()[0]
    negative = cur.execute(
        "SELECT COUNT(*) FROM wallet_features WHERE funding_to_first_trade_seconds < 0"
    ).fetchone()[0]
    capped = cur.execute(
        "SELECT COUNT(*) FROM wallet_features WHERE funding_capped = 1"
    ).fetchone()[0]
    proxy_funded = cur.execute(
        "SELECT COUNT(*) FROM wallet_features WHERE first_funding_source_type = 'polymarket_proxy'"
    ).fetchone()[0]
    external_funded = cur.execute(
        "SELECT COUNT(*) FROM wallet_features WHERE first_funding_source_type = 'external'"
    ).fetchone()[0]

    latency_rows = cur.execute("""
        SELECT funding_to_first_trade_seconds / 3600.0
        FROM wallet_features
        WHERE funding_to_first_trade_seconds IS NOT NULL
          AND funding_to_first_trade_seconds >= 0
        ORDER BY funding_to_first_trade_seconds
    """).fetchall()
    lat = [r[0] for r in latency_rows]

    def pct(p):
        if not lat:
            return float("nan")
        idx = min(int(p / 100 * len(lat)), len(lat) - 1)
        return lat[idx]

    print("\n=== wallet_features summary ===")
    print(f"  total wallets:                {total}")
    print(f"  no funding event found:       {no_funding}  (traded but no non-infra IN)")
    print(f"  negative funding latency:     {negative}  (traded before first visible funding)")
    print(f"  funding pull capped at 10k:   {capped}")
    print(f"  first funding = proxy:        {proxy_funded}")
    print(f"  first funding = external:     {external_funded}")
    if lat:
        print("\n  funding-to-first-trade latency (hours, non-negative only):")
        print(f"    p10={pct(10):.1f}  p25={pct(25):.1f}  p50={pct(50):.1f}  "
              f"p75={pct(75):.1f}  p90={pct(90):.1f}  max={lat[-1]:.1f}")

    print("\n  fastest 10 funding-to-first-trade wallets (potential signals):")
    rows = cur.execute("""
        SELECT proxy_wallet, funding_to_first_trade_seconds / 3600.0 AS hrs,
               total_notional_usdc, first_funding_source_type
        FROM wallet_features
        WHERE funding_to_first_trade_seconds >= 0
        ORDER BY funding_to_first_trade_seconds ASC
        LIMIT 10
    """).fetchall()
    for w, hrs, notional, src in rows:
        print(f"    {w[:14]}...  {hrs:8.2f}h  ${notional:>12,.0f}  {src}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the wallet_features table.")
    parser.add_argument("--db", default="data/trades.db")
    parser.add_argument("--force", action="store_true", help="Rebuild even if table exists.")
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
