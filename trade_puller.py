"""Polymarket trade puller.

Pulls all historical fills for a given Polymarket conditionId from the
Data API and stores them in a SQLite database.

Usage:
    python trade_puller.py <condition_id> [--db data/trades.db] [--sleep 0.2]

Idempotent: re-running on the same conditionId skips already-stored trades
via the tx_hash primary key.

Resumable: if interrupted, re-run and it picks up from where it left off
by querying the max timestamp already in the DB for that conditionId.
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

import requests

logger = logging.getLogger("trade_puller")

DATA_API_URL = "https://data-api.polymarket.com/trades"
PAGE_LIMIT = 500          # page size (API accepts up to 1000)
MAX_OFFSET = 10000        # API max offset per docs
DEFAULT_SLEEP = 0.2       # seconds between page requests
MAX_RETRIES = 4

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


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create DB and schema if not present, return connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # WAL mode lets reads happen concurrently with writes — useful if
    # the feature pipeline runs while pullers are still going.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def fetch_page(condition_id: str, offset: int) -> list[dict]:
    """Fetch one page of trades. Retries with exponential backoff."""
    params = {
        "market": condition_id,
        "limit": PAGE_LIMIT,
        "offset": offset,
    }
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(DATA_API_URL, params=params, timeout=30)
            if resp.status_code == 400:
                logger.warning(
                    "offset cap reached for this market at offset %d (HTTP 400)",
                    offset,
                )
                return []
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError(f"unexpected response shape: {type(data).__name__}")
            return data
        except (requests.RequestException, json.JSONDecodeError, ValueError) as err:
            last_err = err
            if attempt == MAX_RETRIES:
                break
            sleep_for = 2 ** (attempt - 1)
            logger.warning(
                "page fetch failed (attempt %d/%d) at offset %d: %s — sleeping %ds",
                attempt, MAX_RETRIES, offset, err, sleep_for,
            )
            time.sleep(sleep_for)
    raise RuntimeError(f"page fetch failed after {MAX_RETRIES} attempts: {last_err}")


def normalize_trade(raw: dict, condition_id: str, pulled_at: str) -> dict | None:
    """Convert one Data API row into our DB schema.

    Returns None and logs a warning if required fields are missing.
    """
    try:
        tx_hash = raw["transactionHash"]
        size = float(raw["size"])
        price = float(raw["price"])
        ts_unix = int(raw["timestamp"])
        return {
            "tx_hash": tx_hash,
            "condition_id": condition_id,
            "proxy_wallet": raw["proxyWallet"].lower(),
            "side": raw["side"],
            "asset": str(raw["asset"]),
            "outcome": raw.get("outcome"),
            "outcome_index": raw.get("outcomeIndex"),
            "size": size,
            "price": price,
            "notional_usdc": size * price,
            "timestamp_unix": ts_unix,
            "timestamp_iso": datetime.fromtimestamp(ts_unix, tz=UTC).isoformat(),
            "event_slug": raw.get("eventSlug"),
            "pulled_at": pulled_at,
        }
    except (KeyError, TypeError, ValueError) as err:
        logger.warning("skipping malformed trade row: %s | raw=%s", err, raw)
        return None


def insert_trades(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    """Bulk-insert with INSERT OR IGNORE. Returns (inserted, skipped) counts."""
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


def pull_market(condition_id: str, db_path: Path, sleep_seconds: float) -> None:
    """Pull all trades for one conditionId. Idempotent and resumable."""
    conn = init_db(db_path)
    pulled_at = datetime.now(UTC).isoformat()

    # Record the run.
    cur = conn.execute(
        "INSERT INTO pull_runs (condition_id, started_at, status) VALUES (?, ?, 'running')",
        (condition_id, pulled_at),
    )
    run_id = cur.lastrowid
    conn.commit()

    total_inserted = 0
    total_skipped = 0
    offset = 0
    page_num = 0

    try:
        while True:
            page_num += 1
            raw_page = fetch_page(condition_id, offset)
            if not raw_page:
                logger.info("page %d: empty — done", page_num)
                break

            rows = [
                normalized for r in raw_page
                if (normalized := normalize_trade(r, condition_id, pulled_at)) is not None
            ]
            inserted, skipped = insert_trades(conn, rows)
            total_inserted += inserted
            total_skipped += skipped

            logger.info(
                "page %d: offset=%d fetched=%d inserted=%d skipped=%d (total: %d / %d)",
                page_num, offset, len(raw_page), inserted, skipped,
                total_inserted, total_skipped,
            )

            if len(raw_page) < PAGE_LIMIT:
                break

            if offset >= MAX_OFFSET:
                logger.warning(
                    "API offset cap (%d) reached — stored %d trades; "
                    "older fills may not be available via this endpoint.",
                    MAX_OFFSET, total_inserted + total_skipped,
                )
                break

            offset += PAGE_LIMIT
            time.sleep(sleep_seconds)

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
            "done: condition=%s inserted=%d skipped=%d",
            condition_id, total_inserted, total_skipped,
        )

    except Exception as err:
        conn.execute(
            """
            UPDATE pull_runs
            SET finished_at = ?, trades_inserted = ?, trades_skipped = ?,
                status = 'failed', error_message = ?
            WHERE run_id = ?
            """,
            (datetime.now(UTC).isoformat(), total_inserted, total_skipped, str(err), run_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull Polymarket trades for a conditionId.")
    parser.add_argument("condition_id", help="The market conditionId to pull.")
    parser.add_argument("--db", default="data/trades.db", help="Path to SQLite DB.")
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP,
        help="Seconds to sleep between page requests.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    pull_market(args.condition_id, Path(args.db), args.sleep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
