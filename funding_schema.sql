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
