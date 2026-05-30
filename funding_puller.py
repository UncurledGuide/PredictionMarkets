"""Polymarket proxy wallet funding puller.

For every distinct proxy_wallet appearing in the trades table, pulls its
USDC transfer history (both USDC.e bridged and native USDC) from the
Etherscan V2 API (Polygon, chainid=137) and stores transfers in a
funding_events table.

This gives us per-wallet funding signals: where the money came from,
when it arrived, withdrawal patterns. Replaces the original detector's
broken nonce-based "freshness" check with funding-graph analysis.

Usage:
    python funding_puller.py [--db data/trades.db] [--sleep 0.25]
                             [--only-wallet 0xabc...] [--force]

Requires ETHERSCAN_API_KEY in .env or environment.

Idempotent: per (wallet, token_contract) pair, success runs are skipped
on subsequent invocations. Use --force to re-pull.

Resumable: failed wallets are retried on next run automatically.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional; env var can be set manually

logger = logging.getLogger("funding_puller")

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
POLYGON_CHAIN_ID = 137
PAGE_LIMIT = 1000             # safe under both current (10k) and post-July-1 (1k) caps
DEFAULT_SLEEP = 0.25          # ~4 req/sec, under the 5 req/sec free-tier ceiling
MAX_RETRIES = 4
MAX_PAGES_PER_WALLET = 50     # safety stop: 50k transfers per wallet is plenty

# USDC contracts on Polygon. Both used by Polymarket users at different times.
USDC_CONTRACTS = {
    "USDC.e": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",   # bridged USDC
    "USDC":   "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",   # native USDC
}
USDC_DECIMALS = 6  # both variants are 6-decimal

SCHEMA = """
CREATE TABLE IF NOT EXISTS funding_events (
    tx_hash         TEXT NOT NULL,
    proxy_wallet    TEXT NOT NULL,
    block_number    INTEGER NOT NULL,
    timestamp_unix  INTEGER NOT NULL,
    timestamp_iso   TEXT NOT NULL,
    direction       TEXT NOT NULL,
    counterparty    TEXT NOT NULL,
    amount_usdc     REAL NOT NULL,
    token_contract  TEXT NOT NULL,
    token_symbol    TEXT NOT NULL,
    pulled_at       TEXT NOT NULL,
    PRIMARY KEY (tx_hash, proxy_wallet, direction, token_contract)
);

CREATE INDEX IF NOT EXISTS idx_funding_wallet     ON funding_events(proxy_wallet);
CREATE INDEX IF NOT EXISTS idx_funding_timestamp  ON funding_events(timestamp_unix);
CREATE INDEX IF NOT EXISTS idx_funding_counter    ON funding_events(counterparty);
CREATE INDEX IF NOT EXISTS idx_funding_direction  ON funding_events(direction);

CREATE TABLE IF NOT EXISTS funding_pull_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_wallet    TEXT NOT NULL,
    token_symbol    TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    events_inserted INTEGER,
    events_skipped  INTEGER,
    pages_fetched   INTEGER,
    status          TEXT NOT NULL,
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_funding_runs_wallet ON funding_pull_runs(proxy_wallet, token_symbol);
CREATE INDEX IF NOT EXISTS idx_funding_runs_status ON funding_pull_runs(status);
"""


@dataclass
class PullResult:
    inserted: int
    skipped: int
    pages: int
    status: str           # 'success', 'no_data', 'failed'
    error: str | None = None


def _needs_funding_migration(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='funding_events'"
    ).fetchone()
    if not row:
        return False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(funding_events)")}
    return "token_symbol" not in cols


def init_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"DB not found at {db_path}. Run trade_puller.py first to populate trades."
        )
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    if _needs_funding_migration(conn):
        logger.warning("old funding schema detected — dropping funding_events / funding_pull_runs")
        conn.executescript("DROP TABLE IF EXISTS funding_events; DROP TABLE IF EXISTS funding_pull_runs;")
        conn.commit()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_distinct_wallets(
    conn: sqlite3.Connection,
    min_markets: int = 1,
    min_notional: float = 0.0,
) -> list[str]:
    """Read unique proxy_wallet addresses from trades, with optional filters.

    min_markets   — only include wallets trading this many distinct markets
    min_notional  — only count trades above this notional size toward min_markets
    """
    if min_markets > 1 or min_notional > 0:
        cur = conn.execute(
            """
            SELECT proxy_wallet
            FROM trades
            WHERE notional_usdc >= ?
            GROUP BY proxy_wallet
            HAVING COUNT(DISTINCT condition_id) >= ?
            ORDER BY proxy_wallet
            """,
            (min_notional, min_markets),
        )
    else:
        cur = conn.execute(
            "SELECT DISTINCT proxy_wallet FROM trades ORDER BY proxy_wallet"
        )
    return [row[0] for row in cur.fetchall()]


def get_completed_pulls(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Return set of (wallet, token_symbol) pairs already pulled successfully."""
    cur = conn.execute(
        """
        SELECT proxy_wallet, token_symbol
        FROM funding_pull_runs
        WHERE status IN ('success', 'no_data')
        """
    )
    return {(w.lower(), t) for w, t in cur.fetchall()}


def fetch_page(
    api_key: str,
    wallet: str,
    contract: str,
    page: int,
) -> list[dict]:
    """Fetch one page of token transfers from Etherscan V2 for (wallet, contract)."""
    params = {
        "chainid": POLYGON_CHAIN_ID,
        "module": "account",
        "action": "tokentx",
        "contractaddress": contract,
        "address": wallet,
        "page": page,
        "offset": PAGE_LIMIT,
        "sort": "asc",
        "apikey": api_key,
    }
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(ETHERSCAN_V2_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = str(data.get("status", ""))
            result = data.get("result", [])
            if status == "1" and isinstance(result, list):
                return result
            if status == "0" and isinstance(result, list) and not result:
                return []
            msg = data.get("message", "unknown")
            if isinstance(result, str):
                err_text = f"{msg}: {result}"
                if "result window is too large" in err_text.lower():
                    logger.warning(
                        "offset cap reached at page %d for wallet=%s (page*offset > 10000)",
                        page, wallet[:10],
                    )
                    return []
                raise RuntimeError(f"API error: {err_text}")
            raise RuntimeError(f"API error: {msg}")
        except (requests.RequestException, json.JSONDecodeError, ValueError, RuntimeError) as err:
            if "result window is too large" in str(err).lower():
                logger.warning(
                    "offset cap reached at page %d for wallet=%s (page*offset > 10000)",
                    page, wallet[:10],
                )
                return []
            last_err = err
            if attempt == MAX_RETRIES:
                break
            sleep_for = 2 ** (attempt - 1)
            logger.warning(
                "fetch failed (attempt %d/%d) wallet=%s contract=%s page=%d: %s — sleeping %ds",
                attempt, MAX_RETRIES, wallet[:10], contract[:10], page, err, sleep_for,
            )
            time.sleep(sleep_for)
    raise RuntimeError(f"fetch failed after {MAX_RETRIES} attempts: {last_err}")


def normalize_transfer(raw: dict, wallet: str, symbol: str, pulled_at: str) -> dict | None:
    """Convert one Etherscan tokentx row to our schema. Returns None on bad row."""
    try:
        tx_hash = raw["hash"]
        block_number = int(raw["blockNumber"])
        ts_unix = int(raw["timeStamp"])
        from_addr = raw["from"].lower()
        to_addr = raw["to"].lower()
        wallet_lc = wallet.lower()
        if to_addr == wallet_lc and from_addr != wallet_lc:
            direction = "IN"
            counterparty = from_addr
        elif from_addr == wallet_lc and to_addr != wallet_lc:
            direction = "OUT"
            counterparty = to_addr
        elif from_addr == wallet_lc and to_addr == wallet_lc:
            direction = "IN"
            counterparty = wallet_lc
        else:
            logger.warning(
                "transfer doesn't involve wallet %s: from=%s to=%s tx=%s",
                wallet_lc, from_addr, to_addr, tx_hash,
            )
            return None

        raw_value = int(raw["value"])
        decimals = int(raw.get("tokenDecimal") or USDC_DECIMALS)
        amount_usdc = raw_value / (10 ** decimals)

        return {
            "tx_hash": tx_hash,
            "proxy_wallet": wallet_lc,
            "block_number": block_number,
            "timestamp_unix": ts_unix,
            "timestamp_iso": datetime.fromtimestamp(ts_unix, tz=UTC).isoformat(),
            "direction": direction,
            "counterparty": counterparty,
            "amount_usdc": amount_usdc,
            "token_contract": raw.get("contractAddress", "").lower(),
            "token_symbol": symbol,
            "pulled_at": pulled_at,
        }
    except (KeyError, TypeError, ValueError) as err:
        logger.warning("skipping malformed transfer row: %s | raw=%s", err, raw)
        return None


def insert_events(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    if not rows:
        return 0, 0
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO funding_events (
            tx_hash, proxy_wallet, block_number, timestamp_unix, timestamp_iso,
            direction, counterparty, amount_usdc, token_contract, token_symbol, pulled_at
        ) VALUES (
            :tx_hash, :proxy_wallet, :block_number, :timestamp_unix, :timestamp_iso,
            :direction, :counterparty, :amount_usdc, :token_contract, :token_symbol, :pulled_at
        )
        """,
        rows,
    )
    conn.commit()
    inserted = conn.total_changes - before
    return inserted, len(rows) - inserted


def pull_wallet_contract(
    conn: sqlite3.Connection,
    api_key: str,
    wallet: str,
    symbol: str,
    contract: str,
    sleep_seconds: float,
) -> PullResult:
    """Pull all USDC transfers for one (wallet, contract) pair."""
    pulled_at = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO funding_pull_runs (proxy_wallet, token_symbol, started_at, status)
        VALUES (?, ?, ?, 'running')
        """,
        (wallet.lower(), symbol, pulled_at),
    )
    run_id = cur.lastrowid
    conn.commit()

    total_inserted = 0
    total_skipped = 0
    pages_fetched = 0

    try:
        for page in range(1, MAX_PAGES_PER_WALLET + 1):
            raw_page = fetch_page(api_key, wallet, contract, page)
            pages_fetched += 1

            if not raw_page:
                break

            rows = [
                norm for r in raw_page
                if (norm := normalize_transfer(r, wallet, symbol, pulled_at)) is not None
            ]
            inserted, skipped = insert_events(conn, rows)
            total_inserted += inserted
            total_skipped += skipped

            if len(raw_page) < PAGE_LIMIT:
                break

            time.sleep(sleep_seconds)
        else:
            logger.warning(
                "hit MAX_PAGES_PER_WALLET (%d) for wallet=%s contract=%s",
                MAX_PAGES_PER_WALLET, wallet[:10], symbol,
            )

        status = "success" if (total_inserted + total_skipped) > 0 else "no_data"
        result = PullResult(total_inserted, total_skipped, pages_fetched, status)

    except Exception as err:
        result = PullResult(total_inserted, total_skipped, pages_fetched, "failed", str(err))

    conn.execute(
        """
        UPDATE funding_pull_runs
        SET finished_at = ?, events_inserted = ?, events_skipped = ?,
            pages_fetched = ?, status = ?, error_message = ?
        WHERE run_id = ?
        """,
        (
            datetime.now(UTC).isoformat(),
            result.inserted, result.skipped, result.pages,
            result.status, result.error, run_id,
        ),
    )
    conn.commit()
    return result


def run(
    db_path: Path,
    api_key: str,
    sleep_seconds: float,
    only_wallet: str | None = None,
    min_markets: int = 1,
    min_notional: float = 0.0,
    force: bool = False,
) -> None:
    conn = init_db(db_path)

    wallets = (
        [only_wallet.lower()] if only_wallet
        else get_distinct_wallets(conn, min_markets=min_markets, min_notional=min_notional)
    )
    completed = set() if force else get_completed_pulls(conn)
    total_pairs = len(wallets) * len(USDC_CONTRACTS)
    pairs_to_pull = sum(
        1 for w in wallets for sym in USDC_CONTRACTS
        if (w.lower(), sym) not in completed
    )
    logger.info(
        "wallets=%d, pairs total=%d, pairs to pull=%d (skipping %d already done)",
        len(wallets), total_pairs, pairs_to_pull, total_pairs - pairs_to_pull,
    )

    processed = 0
    summary_inserted = 0
    summary_skipped = 0
    summary_failed = 0

    for wallet in wallets:
        for symbol, contract in USDC_CONTRACTS.items():
            key = (wallet.lower(), symbol)
            if key in completed:
                continue
            processed += 1
            logger.info(
                "[%d/%d] pulling wallet=%s symbol=%s",
                processed, pairs_to_pull, wallet[:10] + "...", symbol,
            )
            result = pull_wallet_contract(
                conn, api_key, wallet, symbol, contract, sleep_seconds,
            )
            summary_inserted += result.inserted
            summary_skipped += result.skipped
            if result.status == "failed":
                summary_failed += 1
                logger.error(
                    "  FAILED wallet=%s symbol=%s: %s",
                    wallet[:10], symbol, result.error,
                )
            else:
                logger.info(
                    "  %s: inserted=%d skipped=%d pages=%d",
                    result.status, result.inserted, result.skipped, result.pages,
                )
            time.sleep(sleep_seconds)

    logger.info(
        "done. total_inserted=%d total_skipped=%d failed_pairs=%d",
        summary_inserted, summary_skipped, summary_failed,
    )
    if summary_failed > 0:
        logger.info("re-run the script to retry failed pairs.")
    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull USDC funding history for Polymarket proxy wallets.")
    parser.add_argument("--db", default="data/trades.db", help="Path to SQLite DB (must already contain trades).")
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP,
                        help="Seconds between API requests (default 0.25 = 4 req/s).")
    parser.add_argument("--only-wallet", default=None,
                        help="Only pull this one wallet (useful for debugging).")
    parser.add_argument("--min-markets", type=int, default=1,
                        help="Only pull wallets trading at least this many distinct markets.")
    parser.add_argument("--min-notional", type=float, default=0.0,
                        help="Only count trades above this notional size toward --min-markets.")
    parser.add_argument("--force", action="store_true",
                        help="Re-pull all wallets even if marked completed.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    api_key = os.environ.get("ETHERSCAN_API_KEY") or os.environ.get("POLYGONSCAN_API_KEY")
    if not api_key:
        logger.error("ETHERSCAN_API_KEY not set. Add it to .env or export it.")
        return 1

    run(
        db_path=Path(args.db),
        api_key=api_key,
        sleep_seconds=args.sleep,
        only_wallet=args.only_wallet,
        min_markets=args.min_markets,
        min_notional=args.min_notional,
        force=args.force,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
