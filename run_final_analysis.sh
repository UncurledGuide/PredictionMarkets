#!/bin/bash
cd /Users/humza/Polymarket_Insider_Trading/PredictionMarkets
source .venv/bin/activate

echo "=== Waiting for trade_labels to finish ==="
while true; do
    COUNT=$(sqlite3 data/trades.db "SELECT COUNT(*) FROM trade_labels;" 2>/dev/null || echo 0)
    if [ "$COUNT" -gt 0 ]; then
        echo "trade_labels done: $COUNT rows"
        break
    fi
    echo "$(date): trade_labels still running (0 rows)... waiting 30s"
    sleep 30
done

echo ""
echo "=== Running cross_market_features (fast version) ==="
python build_cross_market_features.py --force 2>&1 | tee data/cross_market2.log
echo "cross_market_features done."

echo ""
echo "=== Running final analysis ==="
mkdir -p results
python analyze_predictive_power.py 2>&1 | tee results/analysis.log
echo ""
echo "=== ALL DONE. Results in results/analysis.log ==="
