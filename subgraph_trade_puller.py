"""Polymarket subgraph trade puller.

Pulls full historical OrderFilled events from the Goldsky orderbook subgraph
(no ~3,500 REST cap) and stores them in the same SQLite schema as trade_puller.py.

Usage:
    python subgraph_trade_puller.py <condition_id> [--db data/trades.db]
    python subgraph_trade_puller.py --all-markets [--db data/trades.db]
    python subgraph_trade_puller.py --check-alive

Idempotent via INSERT OR IGNORE on tx_hash (subgraph fill id).
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger("subgraph_trade_puller")

SUBGRAPH_ENDPOINT = (
    "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw"
    "/subgraphs/orderbook-subgraph/0.0.1/gn"
)

PILOT_CONDITION_ID = (
    "0x260fd9d6b10746909a26c2af7a68b409f757c95a07dc57ddd480774a36c8399b"
)

EXCHANGE_ADDRESS = "0xc5d563a36ae78145c45a50134d48a1215220f80a"
USDC_ASSET_ID = "0"
AMOUNT_SCALE = 1_000_000
DEFAULT_SLEEP = 0.15
DEFAULT_DB = Path("data/trades.db")
DEFAULT_PARQUET = Path("data/all_resolved_markets.parquet")

# Same schema as trade_puller.py
SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    tx_hash         TEXT PRIMARY KEY,
    condition_id    TEXT NOT NULL,
    proxy_wallet    TEXT NOT NULL,
    side            TEXT NOT NULL,
    asset           TEXT NOT NULL,
    outcome         TEXT,
    outcome_index   INTEGER,
    size            REAL NOT NULL,
    price           REAL NOT NULL,
    notional_usdc   REAL NOT NULL,
    timestamp_unix  INTEGER NOT NULL,
    timestamp_iso   TEXT NOT NULL,
    event_slug      TEXT,
    pulled_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_condition  ON trades(condition_id);
CREATE INDEX IF NOT EXISTS idx_trades_wallet     ON trades(proxy_wallet);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp  ON trades(timestamp_unix);

CREATE TABLE IF NOT EXISTS pull_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    trades_inserted INTEGER,
    trades_skipped  INTEGER,
    status          TEXT NOT NULL,
    error_message   TEXT
);
"""

FILL_QUERY = """
query Fills($tokens: [String!]!, $lastTs: BigInt!) {
  orderFilledEvents(
    first: 1000
    orderBy: timestamp
    orderDirection: asc
    where: {
      or: [
        { makerAssetId_in: $tokens, timestamp_gt: $lastTs }
        { takerAssetId_in: $tokens, timestamp_gt: $lastTs }
      ]
    }
  ) {
    id
    transactionHash
    timestamp
    maker
    taker
    makerAssetId
    takerAssetId
    makerAmountFilled
    takerAmountFilled
  }
}
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def graphql(
    endpoint: str,
    query: str,
    variables: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(endpoint, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]


def check_subgraph_alive(endpoint: str) -> dict[str, Any]:
    return graphql(endpoint, "{ _meta { block { number } hasIndexingErrors } }")["_meta"]


def fetch_market_meta(condition_id: str) -> dict[str, Any]:
    resp = requests.get(
        f"https://clob.polymarket.com/markets/{condition_id}",
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def token_outcome_map(market: dict[str, Any], condition_id: str) -> dict[str, tuple[str, int]]:
    mapping: dict[str, tuple[str, int]] = {}
    for idx, tok in enumerate(market.get("tokens") or []):
        token_id = str(tok["token_id"])
        outcome = tok.get("outcome") or ("Yes" if idx == 0 else "No")
        mapping[token_id] = (outcome, idx)
    if not mapping:
        raise ValueError(f"No tokens found for condition {condition_id}")
    return mapping


def normalize_fill(
    ev: dict[str, Any],
    condition_id: str,
    token_map: dict[str, tuple[str, int]],
    event_slug: str | None,
    pulled_at: str,
) -> dict[str, Any] | None:
    """Map one OrderFilledEvent to a trades row (same shape as trade_puller.normalize_trade)."""
    try:
        maker = ev["maker"].lower()
        taker = ev["taker"].lower()
        maker_asset = ev["makerAssetId"]
        taker_asset = ev["takerAssetId"]
        maker_amt = int(ev["makerAmountFilled"])
        taker_amt = int(ev["takerAmountFilled"])

        if maker_asset == USDC_ASSET_ID and taker_asset in token_map:
            asset = taker_asset
            usdc_amt, share_amt = maker_amt, taker_amt
            if taker == EXCHANGE_ADDRESS:
                wallet, side = maker, "BUY"
            else:
                wallet, side = taker, "SELL"
        elif taker_asset == USDC_ASSET_ID and maker_asset in token_map:
            asset = maker_asset
            share_amt, usdc_amt = maker_amt, taker_amt
            if maker == EXCHANGE_ADDRESS:
                wallet, side = taker, "BUY"
            else:
                wallet, side = maker, "SELL"
        else:
            return None

        if wallet == EXCHANGE_ADDRESS or share_amt <= 0:
            return None

        outcome, outcome_index = token_map[asset]
        size = share_amt / AMOUNT_SCALE
        price = usdc_amt / share_amt
        ts_unix = int(ev["timestamp"])

        return {
            # Subgraph fill id (tx + order hash). Unique per fill; REST uses transactionHash
            # only but one on-chain tx can emit multiple user fills.
            "tx_hash": ev["id"],
            "condition_id": condition_id,
            "proxy_wallet": wallet,
            "side": side,
            "asset": asset,
            "outcome": outcome,
            "outcome_index": outcome_index,
            "size": size,
            "price": price,
            "notional_usdc": size * price,
            "timestamp_unix": ts_unix,
            "timestamp_iso": datetime.fromtimestamp(ts_unix, tz=UTC).isoformat(),
            "event_slug": event_slug,
            "pulled_at": pulled_at,
        }
    except (KeyError, TypeError, ValueError) as err:
        logger.warning("skipping malformed fill: %s | ev=%s", err, ev)
        return None


def fetch_all_fills(
    endpoint: str,
    token_ids: list[str],
    sleep_seconds: float,
) -> list[dict[str, Any]]:
    all_events: list[dict[str, Any]] = []
    last_ts = 0
    page = 0

    while True:
        page += 1
        data = graphql(
            endpoint,
            FILL_QUERY,
            variables={"tokens": token_ids, "lastTs": str(last_ts)},
        )
        batch = data["orderFilledEvents"]
        if not batch:
            logger.info("page %d: empty — done", page)
            break

        all_events.extend(batch)
        last_ts = int(batch[-1]["timestamp"])
        logger.info("page %d: fetched %d (total %d)", page, len(batch), len(all_events))

        if len(batch) < 1000:
            break
        time.sleep(sleep_seconds)

    return all_events


def insert_trades(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    cur = conn.cursor()
    before = conn.total_changes
    cur.executemany(
        """
        INSERT OR IGNORE INTO trades (
            tx_hash, condition_id, proxy_wallet, side, asset, outcome,
            outcome_index, size, price, notional_usdc, timestamp_unix,
            timestamp_iso, event_slug, pulled_at
        ) VALUES (
            :tx_hash, :condition_id, :proxy_wallet, :side, :asset, :outcome,
            :outcome_index, :size, :price, :notional_usdc, :timestamp_unix,
            :timestamp_iso, :event_slug, :pulled_at
        )
        """,
        rows,
    )
    conn.commit()
    inserted = conn.total_changes - before
    skipped = len(rows) - inserted
    return inserted, skipped


def pull_market(
    condition_id: str,
    db_path: Path,
    endpoint: str,
    sleep_seconds: float,
) -> None:
    conn = init_db(db_path)
    pulled_at = datetime.now(UTC).isoformat()

    cur = conn.execute(
        "INSERT INTO pull_runs (condition_id, started_at, status) VALUES (?, ?, 'running')",
        (condition_id, pulled_at),
    )
    run_id = cur.lastrowid
    conn.commit()

    total_inserted = 0
    total_skipped = 0

    try:
        market = fetch_market_meta(condition_id)
        token_map = token_outcome_map(market, condition_id)
        token_ids = list(token_map.keys())
        event_slug = market.get("market_slug")

        events = fetch_all_fills(endpoint, token_ids, sleep_seconds)
        rows = [
            normalized
            for ev in events
            if (normalized := normalize_fill(ev, condition_id, token_map, event_slug, pulled_at))
            is not None
        ]
        inserted, skipped = insert_trades(conn, rows)
        total_inserted += inserted
        total_skipped += skipped

        conn.execute(
            """
            UPDATE pull_runs
            SET finished_at = ?, trades_inserted = ?, trades_skipped = ?, status = 'success'
            WHERE run_id = ?
            """,
            (datetime.now(UTC).isoformat(), total_inserted, total_skipped, run_id),
        )
        conn.commit()
        logger.info(
            "done: condition=%s fetched=%d inserted=%d skipped=%d",
            condition_id, len(rows), total_inserted, total_skipped,
        )

    except Exception as err:
        conn.execute(
            """
            UPDATE pull_runs
            SET finished_at = ?, trades_inserted = ?, trades_skipped = ?,
                status = 'failed', error_message = ?
            WHERE run_id = ?
            """,
            (
                datetime.now(UTC).isoformat(),
                total_inserted,
                total_skipped,
                str(err),
                run_id,
            ),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def load_condition_ids_from_db(db_path: Path, resume: bool = False) -> list[str]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        if resume:
            rows = conn.execute(
                """
                SELECT condition_id FROM markets
                WHERE condition_id NOT IN (
                    SELECT condition_id FROM pull_runs WHERE status = 'success'
                )
                ORDER BY volume_usd DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT condition_id FROM markets ORDER BY volume_usd DESC"
            ).fetchall()
        return [r[0] for r in rows if r[0]]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def load_condition_ids_from_parquet(path: Path) -> list[str]:
    if not path.exists():
        return []
    import pandas as pd

    df = pd.read_parquet(path)
    if "condition_id" not in df.columns:
        return []
    return [str(x) for x in df["condition_id"].dropna().unique()]


def cross_check_rest(condition_id: str, db_path: Path, sample: int = 20) -> None:
    resp = requests.get(
        "https://data-api.polymarket.com/trades",
        params={"market": condition_id, "limit": sample},
        timeout=30,
    )
    resp.raise_for_status()
    rest_trades = resp.json()

    conn = sqlite3.connect(db_path)
    matched = 0
    for rt in rest_trades:
        row = conn.execute(
            """
            SELECT 1 FROM trades
            WHERE condition_id = ?
              AND proxy_wallet = ?
              AND asset = ?
              AND side = ?
              AND ABS(size - ?) < 0.01
              AND ABS(price - ?) < 0.0001
            LIMIT 1
            """,
            (
                condition_id,
                rt["proxyWallet"].lower(),
                str(rt["asset"]),
                rt["side"],
                float(rt["size"]),
                float(rt["price"]),
            ),
        ).fetchone()
        if row:
            matched += 1
    conn.close()
    logger.info("REST cross-check: %d/%d recent trades matched in DB", matched, len(rest_trades))


def print_stats(db_path: Path, condition_id: str | None) -> None:
    conn = sqlite3.connect(db_path)
    if condition_id:
        row = conn.execute(
            "SELECT COUNT(*), MIN(timestamp_iso), MAX(timestamp_iso) "
            "FROM trades WHERE condition_id = ?",
            (condition_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*), MIN(timestamp_iso), MAX(timestamp_iso) FROM trades"
        ).fetchone()
    conn.close()
    print(f"trades: {row[0]}")
    print(f"date range: {row[1]} -> {row[2]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pull Polymarket trades from the orderbook subgraph.",
    )
    parser.add_argument(
        "condition_id",
        nargs="?",
        default=None,
        help="conditionId to pull (omit with --all-markets)",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to SQLite DB.")
    parser.add_argument("--endpoint", default=SUBGRAPH_ENDPOINT)
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    parser.add_argument(
        "--all-markets",
        action="store_true",
        help="Pull every condition_id in the DB markets table or parquet cache",
    )
    parser.add_argument(
        "--parquet",
        default=str(DEFAULT_PARQUET),
        help="Parquet path when markets table is empty",
    )
    parser.add_argument("--check-alive", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip markets that already have a successful pull_run entry (use with --all-markets)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = Path(args.db)

    if args.check_alive:
        meta = check_subgraph_alive(args.endpoint)
        print(json.dumps(meta, indent=2))
        return 0

    if args.verify_only:
        cid = args.condition_id or PILOT_CONDITION_ID
        print_stats(db_path, cid)
        cross_check_rest(cid, db_path)
        return 0

    if args.all_markets:
        condition_ids = load_condition_ids_from_db(db_path, resume=args.resume)
        if not condition_ids:
            condition_ids = load_condition_ids_from_parquet(Path(args.parquet))
        if not condition_ids:
            raise SystemExit(
                "No markets found. Run market_collector.py first, or pass a condition_id."
            )
        label = "remaining" if args.resume else "total"
        logger.info("Pulling %d %s markets", len(condition_ids), label)
    elif args.condition_id:
        condition_ids = [args.condition_id]
    else:
        condition_ids = [PILOT_CONDITION_ID]

    meta = check_subgraph_alive(args.endpoint)
    logger.info(
        "Subgraph block=%s hasIndexingErrors=%s",
        meta["block"]["number"],
        meta["hasIndexingErrors"],
    )

    failed: list[str] = []
    for i, cid in enumerate(condition_ids, 1):
        logger.info("[%d/%d] %s", i, len(condition_ids), cid)
        try:
            pull_market(cid, db_path, args.endpoint, args.sleep)
        except Exception as err:
            logger.warning("skipping %s after error: %s", cid, err)
            failed.append(cid)
            time.sleep(2)

    if failed:
        logger.warning("%d/%d markets failed: %s", len(failed), len(condition_ids), failed)

    if len(condition_ids) == 1:
        print_stats(db_path, condition_ids[0])
        cross_check_rest(condition_ids[0], db_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
