# Polymarket research pipeline

Pull resolved markets, trade fills, USDC funding history, and build wallet/trade-level features for modeling.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add ETHERSCAN_API_KEY
```

## Pipeline (run in order)

### 1. Market cache + keyword filter

```bash
# Fetch ~20k closed markets from Gamma → data/all_resolved_markets.parquet
python market_collector.py --keywords fomc,powell,fed --refresh-cache

# Look up conditionId for a slug
python lookup_market.py will-fed-cut-interest-rates-3-times-by-dec-meeting
```

### 2. Trades

**REST (capped ~3,500 recent fills per market):**

```bash
python trade_puller.py <condition_id>
python view_trades.py
```

**Subgraph (full on-chain history — recommended):**

```bash
python subgraph_trade_puller.py <condition_id>
python subgraph_trade_puller.py --check-alive
python subgraph_trade_puller.py --verify-only <condition_id>

# After market_collector.py populated `markets` / parquet:
python subgraph_trade_puller.py --all-markets
```

Pilot market: `0x260fd9d6b10746909a26c2af7a68b409f757c95a07dc57ddd480774a36c8399b`
(Fed 3 cuts 2024). Verified: ~41k fills, 2024-03-21 → 2024-12-18, 20/20 REST overlap.

### 3. Funding (per-wallet USDC on Polygon)

```bash
python funding_puller.py
python funding_puller.py --only-wallet 0x... --force
```

### 4. Features + labels

```bash
python build_wallet_features.py --force
python build_features.py --force
python build_trade_labels.py --force
```

Train on: `features JOIN trade_labels ON tx_hash`

## Scripts

| Script | Purpose |
|--------|---------|
| `market_collector.py` | Gamma `/markets` → parquet + keyword filter |
| `lookup_market.py` | Slug → `conditionId` from parquet |
| `trade_puller.py` | Data API fills → `data/trades.db` (recent only) |
| `subgraph_trade_puller.py` | Goldsky orderbook subgraph → `data/trades.db` (full history) |
| `funding_puller.py` | Etherscan USDC transfers → same DB |
| `build_wallet_features.py` | One row per wallet |
| `build_features.py` | One row per trade (ex-ante + post) |
| `build_trade_labels.py` | FIFO PnL labels (y) per trade |
| `view_trades.py` | Preview trades table |
| `polymarket_history.py` | CLOB price history (optional) |

## Data layout

```
data/
  all_resolved_markets.parquet   # market metadata cache
  trades.db                      # trades, funding_events, wallet_features, features
```

Large/generated files are gitignored; rebuild locally with the scripts above.

## API keys

- **Gamma / Data API** — no key required
- **Etherscan V2** (Polygon) — `ETHERSCAN_API_KEY` in `.env` for `funding_puller.py`
