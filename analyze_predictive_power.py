"""Predictive Power Analysis — Do Informed Traders Forecast Market Outcomes?

This script answers the core research question:
  "Do wallets flagged as potential insider traders enter positions at prices
   that systematically undervalue the eventual resolution outcome?"

Method:
  1. Build the informed cohort: wallets with strong cross-market signals
     (cluster correct rate >= 0.67 on >= 3 prior markets) AND high FIFO PnL
  2. For each informed trade on a resolved market, compute:
       - direction: did they bet YES or NO?
       - entry_price: what they paid
       - resolution: did the market resolve YES or NO?
       - directional_edge: if they bet YES and it resolved YES,
         edge = (1.0 - entry_price)  [they got a "bargain"]
         if they bet YES and resolved NO, edge = (0.0 - entry_price)  [they were wrong]
  3. Compare mean directional edge of informed vs. random wallets
  4. Compute t-test p-value for the difference
  5. Show edge by market cluster (which geopolitical events had clearest signals)
  6. Plot price-at-entry distribution for informed vs. uninformed on winning bets

Usage:
    python analyze_predictive_power.py [--db data/trades.db] [--min-cluster-correct 0.6]
                                       [--min-cluster-prior 3] [--min-pnl 100]
                                       [--output results/]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict
import json

logger = logging.getLogger("analyze_predictive_power")

# ─────────────────────────────────────────────────────────────
# Config defaults
# ─────────────────────────────────────────────────────────────
MIN_CLUSTER_CORRECT   = 0.60   # cross-market correct rate threshold
MIN_CLUSTER_PRIOR     = 3      # minimum prior resolved markets in cluster
MIN_PNL               = 100.0  # FIFO PnL threshold for label=1
MIN_NOTIONAL_PER_TRADE = 1000.0  # only analyze trades >= $1k
MIN_TRADES_FOR_WALLET = 3      # wallet must have >= this many qualifying trades

KNOWN_INFRA = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",  # CTF Exchange (market maker)
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
    "0x0000000000000000000000000000000000000000",
}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def pct(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    idx = min(int(p / 100 * len(s)), len(s) - 1)
    return s[idx]


def ttest_1samp_manual(vals: list[float], popmean: float = 0.0) -> tuple[float, float]:
    """One-sample t-test: H0 mean == popmean.  Returns (t_stat, p_value approx)."""
    import math
    n = len(vals)
    if n < 2:
        return float("nan"), float("nan")
    mean = sum(vals) / n
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    se = math.sqrt(var / n)
    if se == 0:
        return float("nan"), float("nan")
    t = (mean - popmean) / se
    # Approximate p-value via normal CDF for large n (n > 30 is fine)
    # Two-tailed
    import math
    z = abs(t)
    p = 2 * (1 - _norm_cdf(z))
    return t, p


def _norm_cdf(z: float) -> float:
    """Approximation of standard normal CDF."""
    import math
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def ttest_2samp_manual(a: list[float], b: list[float]) -> tuple[float, float]:
    """Welch's t-test. Returns (t_stat, approx_p_value)."""
    import math
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float("nan"), float("nan")
    ma = sum(a) / na
    mb = sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return float("nan"), float("nan")
    t = (ma - mb) / se
    z = abs(t)
    p = 2 * (1 - _norm_cdf(z))
    return t, p


# ─────────────────────────────────────────────────────────────
# Main analysis
# ─────────────────────────────────────────────────────────────

def run(
    db_path: Path,
    min_cluster_correct: float,
    min_cluster_prior: int,
    min_pnl: float,
    min_notional: float,
    output_dir: Path,
) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row

    # ── 1. Check which tables exist ───────────────────────────
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    logger.info("Tables present: %s", sorted(tables))

    has_cross = "cross_market_features" in tables
    has_labels = "trade_labels" in tables
    has_features = "features" in tables

    if not has_features:
        raise RuntimeError("features table missing — run build_features.py first")
    if not has_labels:
        raise RuntimeError("trade_labels table missing — run build_trade_labels.py first")

    # ── 2. Load resolution outcomes ──────────────────────────
    resolution = {}
    for r in conn.execute("SELECT condition_id, resolution_outcome FROM markets"):
        res = (r["resolution_outcome"] or "").upper()
        if res in ("YES", "NO"):
            resolution[r["condition_id"]] = res

    logger.info("Loaded resolution outcomes for %d markets", len(resolution))

    # ── 3. Identify informed wallet cohort ───────────────────
    informed_wallets: set[str] = set()

    if has_cross and min_cluster_prior > 0:
        # Use cross-market signal: wallets with high cluster correct rate
        rows = conn.execute(f"""
            SELECT DISTINCT proxy_wallet
            FROM cross_market_features
            WHERE exante_wallet_cluster_n_prior >= {min_cluster_prior}
              AND exante_wallet_cluster_correct_rate >= {min_cluster_correct}
              AND proxy_wallet NOT IN ({','.join('?' for _ in KNOWN_INFRA)})
        """, list(KNOWN_INFRA)).fetchall()
        cross_market_wallets = {r[0] for r in rows}
        logger.info("Cross-market signal: %d wallets meet cluster correct rate >= %.2f (prior >= %d)",
                    len(cross_market_wallets), min_cluster_correct, min_cluster_prior)
        informed_wallets |= cross_market_wallets

    if has_labels:
        # Also include wallets with high FIFO PnL (label=1)
        rows = conn.execute(f"""
            SELECT DISTINCT proxy_wallet
            FROM trade_labels
            WHERE label = 1
              AND wallet_market_pnl_usdc >= {min_pnl}
              AND proxy_wallet NOT IN ({','.join('?' for _ in KNOWN_INFRA)})
        """, list(KNOWN_INFRA)).fetchall()
        pnl_wallets = {r[0] for r in rows}
        logger.info("FIFO PnL signal: %d wallets with label=1", len(pnl_wallets))

        if has_cross:
            # Intersection: both signals agree = highest confidence
            intersection = informed_wallets & pnl_wallets
            logger.info("Intersection (both signals): %d wallets", len(intersection))
            # For now use union; we'll report both
            union = informed_wallets | pnl_wallets
            logger.info("Union (either signal): %d wallets", len(union))
            informed_wallets = intersection if intersection else union
        else:
            informed_wallets = pnl_wallets

    # Remove known infra
    informed_wallets -= KNOWN_INFRA

    logger.info("Final informed cohort: %d wallets", len(informed_wallets))
    if not informed_wallets:
        logger.error("No informed wallets found — check thresholds and available tables")
        conn.close()
        return

    # ── 4. Pull all trades on resolved markets ────────────────
    logger.info("Loading trades on resolved markets (notional >= $%.0f)...", min_notional)

    all_trades = conn.execute(f"""
        SELECT t.tx_hash, t.condition_id, t.proxy_wallet, t.side, t.outcome,
               t.price, t.notional_usdc, t.timestamp_unix
        FROM trades t
        WHERE t.notional_usdc >= {min_notional}
          AND t.condition_id IN ({','.join('?' for _ in resolution)})
    """, list(resolution.keys())).fetchall()

    logger.info("Loaded %d qualifying trades", len(all_trades))

    # ── 5. Compute directional edge per trade ─────────────────
    # directional_edge:
    #   bet YES (BUY YES or SELL NO) on market that resolved YES → edge = 1 - price
    #   bet YES on market that resolved NO                        → edge = 0 - price = -price
    #   bet NO (BUY NO or SELL YES) on market that resolved NO   → edge = 1 - (1-price) = price
    #   bet NO on market that resolved YES                       → edge = -(1 - price)
    # Simplification: oriented_price = direction_of_bet (0..1)
    # If bet was right, edge = 1 - oriented_price
    # If bet was wrong, edge = -oriented_price

    def oriented_price(price: float, side: str, outcome: str) -> float:
        """Return the price the wallet effectively paid to bet on the winning direction."""
        if (outcome or "").upper() == "NO":
            p = 1.0 - price
        else:
            p = price
        if (side or "").upper() == "SELL":
            p = 1.0 - p
        return p

    informed_edges = []
    uninformed_edges = []

    # Also track: by cluster, by market age bucket, by entry price bucket
    cluster_edges: dict[str, list[float]] = defaultdict(list)
    wallet_edges: dict[str, list[float]] = defaultdict(list)
    wallet_is_informed: dict[str, bool] = {}

    cluster_id_map: dict[str, str] = {}  # condition_id → cluster_id
    if has_cross:
        for r in conn.execute("SELECT DISTINCT condition_id, exante_cluster_id FROM cross_market_features"):
            cluster_id_map[r[0]] = r[1]

    skipped_no_resolution = 0
    total_informed_trades = 0
    total_uninformed_trades = 0

    for t in all_trades:
        cid = t["condition_id"]
        wallet = t["proxy_wallet"]
        if wallet in KNOWN_INFRA:
            continue
        res = resolution.get(cid)
        if not res:
            skipped_no_resolution += 1
            continue

        op = oriented_price(t["price"], t["side"], t["outcome"])
        # op is now "price of betting in the direction they chose"
        # If resolution matches their bet direction, it was correct
        # Determine if bet direction = YES or NO
        outcome_upper = (t["outcome"] or "").upper()
        side_upper = (t["side"] or "").upper()

        if side_upper == "BUY":
            bet_direction = outcome_upper  # BUY YES = betting YES, BUY NO = betting NO
        else:  # SELL
            bet_direction = "NO" if outcome_upper == "YES" else "YES"

        correct = (bet_direction == res)
        edge = (1.0 - op) if correct else (0.0 - op)

        cluster = cluster_id_map.get(cid, cid[:16])

        is_informed = wallet in informed_wallets
        wallet_is_informed[wallet] = is_informed
        wallet_edges[wallet].append(edge)
        cluster_edges[cluster].append(edge)

        if is_informed:
            informed_edges.append(edge)
            total_informed_trades += 1
        else:
            uninformed_edges.append(edge)
            total_uninformed_trades += 1

    logger.info("Informed trades: %d  |  Uninformed trades: %d",
                total_informed_trades, total_uninformed_trades)

    # ── 6. Statistical tests ──────────────────────────────────
    inf_mean = sum(informed_edges) / len(informed_edges) if informed_edges else float("nan")
    uninf_mean = sum(uninformed_edges) / len(uninformed_edges) if uninformed_edges else float("nan")
    t_stat, p_val = ttest_2samp_manual(informed_edges, uninformed_edges)

    # One-sample test: is informed mean significantly > 0?
    t_inf, p_inf = ttest_1samp_manual(informed_edges, 0.0)

    # ── 7. Print results ──────────────────────────────────────
    print("\n" + "=" * 70)
    print("  PREDICTIVE POWER ANALYSIS — POLYMARKET GEOPOLITICS")
    print("=" * 70)
    print(f"\n  Informed cohort:       {len(informed_wallets):>8,} wallets")
    print(f"  Informed trades:       {total_informed_trades:>8,}")
    print(f"  Uninformed trades:     {total_uninformed_trades:>8,}")

    print("\n  ── Directional Edge (cents per dollar risked) ──")
    print(f"  Informed mean edge:    {inf_mean*100:>+8.2f}¢  "
          f"(p25={pct(informed_edges,25)*100:+.2f}¢  "
          f"p50={pct(informed_edges,50)*100:+.2f}¢  "
          f"p75={pct(informed_edges,75)*100:+.2f}¢)")
    print(f"  Uninformed mean edge:  {uninf_mean*100:>+8.2f}¢  "
          f"(p25={pct(uninformed_edges,25)*100:+.2f}¢  "
          f"p50={pct(uninformed_edges,50)*100:+.2f}¢  "
          f"p75={pct(uninformed_edges,75)*100:+.2f}¢)")
    print(f"\n  Difference:            {(inf_mean-uninf_mean)*100:>+8.2f}¢")
    print(f"  Welch t-test:          t={t_stat:+.3f}  p={p_val:.4f}", end="")
    if p_val < 0.001:
        print("  ***")
    elif p_val < 0.01:
        print("  **")
    elif p_val < 0.05:
        print("  *")
    else:
        print("  (ns)")
    print(f"\n  Informed vs zero:      t={t_inf:+.3f}  p={p_inf:.4f}", end="")
    if p_inf < 0.001:
        print("  ***")
    elif p_inf < 0.01:
        print("  **")
    elif p_inf < 0.05:
        print("  *")
    else:
        print("  (ns)")

    # ── 8. Top clusters by informed edge ─────────────────────
    # Only show clusters that have >= 10 informed trades
    informed_cluster_edges: dict[str, list[float]] = defaultdict(list)
    for t in all_trades:
        cid = t["condition_id"]
        wallet = t["proxy_wallet"]
        if wallet not in informed_wallets or wallet in KNOWN_INFRA:
            continue
        res = resolution.get(cid)
        if not res:
            continue
        op = oriented_price(t["price"], t["side"], t["outcome"])
        outcome_upper = (t["outcome"] or "").upper()
        side_upper = (t["side"] or "").upper()
        if side_upper == "BUY":
            bet_direction = outcome_upper
        else:
            bet_direction = "NO" if outcome_upper == "YES" else "YES"
        correct = (bet_direction == res)
        edge = (1.0 - op) if correct else (0.0 - op)
        cluster = cluster_id_map.get(cid, cid[:16])
        informed_cluster_edges[cluster].append(edge)

    print("\n  ── Top 15 Clusters by Informed Directional Edge ──")
    print(f"  {'cluster':<40}  {'n_trades':>8}  {'mean_edge':>10}  {'pct_correct':>12}")
    top_clusters = sorted(
        [(k, v) for k, v in informed_cluster_edges.items() if len(v) >= 5],
        key=lambda x: -sum(x[1]) / len(x[1])
    )[:15]
    for cluster, edges in top_clusters:
        mean_e = sum(edges) / len(edges)
        pct_correct = sum(1 for e in edges if e > 0) / len(edges)
        print(f"  {cluster:<40}  {len(edges):>8}  {mean_e*100:>+9.2f}¢  {pct_correct*100:>11.1f}%")

    # ── 9. Entry price distribution on winning trades ─────────
    informed_wins_prices = []
    uninformed_wins_prices = []
    for t in all_trades:
        cid = t["condition_id"]
        wallet = t["proxy_wallet"]
        if wallet in KNOWN_INFRA:
            continue
        res = resolution.get(cid)
        if not res:
            continue
        op = oriented_price(t["price"], t["side"], t["outcome"])
        outcome_upper = (t["outcome"] or "").upper()
        side_upper = (t["side"] or "").upper()
        if side_upper == "BUY":
            bet_direction = outcome_upper
        else:
            bet_direction = "NO" if outcome_upper == "YES" else "YES"
        if bet_direction != res:
            continue  # only winning trades
        entry_price = op  # price paid for the winning direction
        if wallet in informed_wallets:
            informed_wins_prices.append(entry_price)
        else:
            uninformed_wins_prices.append(entry_price)

    print("\n  ── Entry Price on Winning Trades (lower = better information) ──")
    print(f"  Informed wins:    n={len(informed_wins_prices):,}  "
          f"mean={sum(informed_wins_prices)/len(informed_wins_prices)*100:.1f}¢  "
          f"p50={pct(informed_wins_prices,50)*100:.1f}¢" if informed_wins_prices else
          "  Informed wins: none")
    print(f"  Uninformed wins:  n={len(uninformed_wins_prices):,}  "
          f"mean={sum(uninformed_wins_prices)/len(uninformed_wins_prices)*100:.1f}¢  "
          f"p50={pct(uninformed_wins_prices,50)*100:.1f}¢" if uninformed_wins_prices else
          "  Uninformed wins: none")

    t_ep, p_ep = ttest_2samp_manual(informed_wins_prices, uninformed_wins_prices)
    print(f"  Entry price gap:  {(sum(uninformed_wins_prices)/len(uninformed_wins_prices) - sum(informed_wins_prices)/len(informed_wins_prices))*100:+.2f}¢  "
          f"t={t_ep:+.3f}  p={p_ep:.4f}" if (informed_wins_prices and uninformed_wins_prices) else "")

    # ── 10. Top informed wallets ──────────────────────────────
    print("\n  ── Top 20 Informed Wallets by Mean Edge ──")
    print(f"  {'wallet':<20}  {'n_trades':>8}  {'mean_edge':>10}  {'pct_correct':>12}")
    top_wallets = sorted(
        [(w, v) for w, v in wallet_edges.items()
         if wallet_is_informed.get(w) and len(v) >= MIN_TRADES_FOR_WALLET],
        key=lambda x: -sum(x[1]) / len(x[1])
    )[:20]
    for w, edges in top_wallets:
        mean_e = sum(edges) / len(edges)
        pct_correct = sum(1 for e in edges if e > 0) / len(edges)
        print(f"  {w[:18]}...  {len(edges):>8}  {mean_e*100:>+9.2f}¢  {pct_correct*100:>11.1f}%")

    # ── 11. Timing analysis: did informed wallets trade early? ─
    print("\n  ── Trade Timing vs. Resolution (days before resolution) ──")
    print("  (Negative = before resolution, Positive = after)")

    market_resolve_ts: dict[str, int] = {}
    from datetime import datetime as _dt
    for r in conn.execute("SELECT condition_id, end_date FROM markets WHERE end_date IS NOT NULL"):
        try:
            dt = _dt.fromisoformat(str(r[1]).replace("Z", "+00:00"))
            market_resolve_ts[r[0]] = int(dt.timestamp())
        except (ValueError, OSError):
            pass

    informed_lead_days = []
    uninformed_lead_days = []
    for t in all_trades:
        cid = t["condition_id"]
        wallet = t["proxy_wallet"]
        if wallet in KNOWN_INFRA:
            continue
        res_ts = market_resolve_ts.get(cid)
        if not res_ts:
            continue
        lead = (res_ts - t["timestamp_unix"]) / 86400.0
        if lead < 0 or lead > 365:
            continue
        if wallet in informed_wallets:
            informed_lead_days.append(lead)
        else:
            uninformed_lead_days.append(lead)

    if informed_lead_days and uninformed_lead_days:
        print(f"  Informed:    p10={pct(informed_lead_days,10):.1f}d  "
              f"p50={pct(informed_lead_days,50):.1f}d  "
              f"p90={pct(informed_lead_days,90):.1f}d  "
              f"mean={sum(informed_lead_days)/len(informed_lead_days):.1f}d")
        print(f"  Uninformed:  p10={pct(uninformed_lead_days,10):.1f}d  "
              f"p50={pct(uninformed_lead_days,50):.1f}d  "
              f"p90={pct(uninformed_lead_days,90):.1f}d  "
              f"mean={sum(uninformed_lead_days)/len(uninformed_lead_days):.1f}d")
        t_ld, p_ld = ttest_2samp_manual(informed_lead_days, uninformed_lead_days)
        print(f"  Informed trade earlier?  t={t_ld:+.3f}  p={p_ld:.4f}",
              "***" if p_ld < 0.001 else "**" if p_ld < 0.01 else "*" if p_ld < 0.05 else "(ns)")

    # ── 12. Save JSON summary ─────────────────────────────────
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "informed_wallets": len(informed_wallets),
        "informed_trades": total_informed_trades,
        "uninformed_trades": total_uninformed_trades,
        "informed_mean_edge_cents": round(inf_mean * 100, 4) if inf_mean == inf_mean else None,
        "uninformed_mean_edge_cents": round(uninf_mean * 100, 4) if uninf_mean == uninf_mean else None,
        "edge_difference_cents": round((inf_mean - uninf_mean) * 100, 4),
        "welch_t": round(t_stat, 4),
        "welch_p": round(p_val, 6),
        "informed_vs_zero_t": round(t_inf, 4),
        "informed_vs_zero_p": round(p_inf, 6),
        "top_clusters": [
            {
                "cluster": c,
                "n_trades": len(e),
                "mean_edge_cents": round(sum(e) / len(e) * 100, 2),
                "pct_correct": round(sum(1 for x in e if x > 0) / len(e) * 100, 1),
            }
            for c, e in top_clusters
        ],
        "thresholds": {
            "min_cluster_correct": min_cluster_correct,
            "min_cluster_prior": min_cluster_prior,
            "min_pnl": min_pnl,
            "min_notional_per_trade": min_notional,
        },
    }
    out_path = output_dir / "predictive_power_summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"\n  Summary saved to: {out_path}")
    print("=" * 70 + "\n")

    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze whether informed wallets have predictive power over market outcomes."
    )
    parser.add_argument("--db", default="data/trades.db")
    parser.add_argument("--min-cluster-correct", type=float, default=MIN_CLUSTER_CORRECT,
                        help="Cross-market cluster correct rate threshold (default 0.60)")
    parser.add_argument("--min-cluster-prior", type=int, default=MIN_CLUSTER_PRIOR,
                        help="Minimum prior resolved markets in cluster (default 3)")
    parser.add_argument("--min-pnl", type=float, default=MIN_PNL,
                        help="Minimum FIFO PnL for label=1 (default $100)")
    parser.add_argument("--min-notional", type=float, default=MIN_NOTIONAL_PER_TRADE,
                        help="Minimum trade notional to include (default $1000)")
    parser.add_argument("--output", default="results/",
                        help="Output directory for JSON summary")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run(
        db_path=Path(args.db),
        min_cluster_correct=args.min_cluster_correct,
        min_cluster_prior=args.min_cluster_prior,
        min_pnl=args.min_pnl,
        min_notional=args.min_notional,
        output_dir=Path(args.output),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
