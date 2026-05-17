"""Peek at trades stored in data/trades.db."""

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

DEFAULT_DB = Path("data/trades.db")


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview trades in SQLite")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("-n", type=int, default=15, help="Rows to show (default 15)")
    parser.add_argument(
        "--condition-id",
        help="Filter to one market (optional)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"No database at {args.db}")
        print("Run: python trade_puller.py <condition_id>")
        return

    conn = sqlite3.connect(args.db)

    print(f"Database: {args.db}\n")

    runs = pd.read_sql_query(
        "SELECT run_id, condition_id, started_at, finished_at, "
        "trades_inserted, trades_skipped, status "
        "FROM pull_runs ORDER BY run_id DESC LIMIT 5",
        conn,
    )
    if not runs.empty:
        print("=== Recent pull runs ===")
        print(runs.to_string(index=False))
        print()

    where = ""
    params: tuple = ()
    if args.condition_id:
        where = "WHERE condition_id = ?"
        params = (args.condition_id,)

    summary = pd.read_sql_query(
        f"""
        SELECT
            COUNT(*) AS trades,
            COUNT(DISTINCT condition_id) AS markets,
            COUNT(DISTINCT proxy_wallet) AS wallets,
            MIN(timestamp_iso) AS earliest,
            MAX(timestamp_iso) AS latest,
            ROUND(SUM(notional_usdc), 2) AS total_notional_usdc
        FROM trades {where}
        """,
        conn,
        params=params,
    )
    print("=== Summary ===")
    for col in summary.columns:
        print(f"  {col}: {summary.iloc[0][col]}")
    print()

    by_side = pd.read_sql_query(
        f"""
        SELECT side, COUNT(*) AS n, ROUND(SUM(notional_usdc), 2) AS notional
        FROM trades {where}
        GROUP BY side
        """,
        conn,
        params=params,
    )
    print("=== By side ===")
    print(by_side.to_string(index=False))
    print()

    by_outcome = pd.read_sql_query(
        f"""
        SELECT outcome, COUNT(*) AS n, ROUND(AVG(price), 4) AS avg_price
        FROM trades {where}
        GROUP BY outcome
        ORDER BY n DESC
        """,
        conn,
        params=params,
    )
    print("=== By outcome ===")
    print(by_outcome.to_string(index=False))
    print()

    sample = pd.read_sql_query(
        f"""
        SELECT
            timestamp_iso,
            side,
            outcome,
            ROUND(size, 2) AS size,
            ROUND(price, 4) AS price,
            ROUND(notional_usdc, 2) AS notional_usdc,
            substr(proxy_wallet, 1, 10) || '...' AS wallet,
            substr(tx_hash, 1, 14) || '...' AS tx_hash
        FROM trades {where}
        ORDER BY timestamp_unix DESC
        LIMIT ?
        """,
        conn,
        params=params + (args.n,),
    )
    print(f"=== Latest {args.n} trades ===")
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print(sample.to_string(index=False))

    conn.close()


if __name__ == "__main__":
    main()
