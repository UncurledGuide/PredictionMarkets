"""Look up a market slug in the resolved-markets parquet cache."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

DEFAULT_CACHE = Path("data/all_resolved_markets.parquet")


def lookup(slug: str, cache_path: Path = DEFAULT_CACHE) -> None:
    if not cache_path.exists():
        print(f"No cache at {cache_path}")
        print("Run: python market_collector.py --keywords ... --refresh-cache")
        return

    df = pd.read_parquet(cache_path)
    match = df[df["slug"] == slug]
    if match.empty:
        print(f"No market found for slug: {slug}")
        return

    row = match.iloc[0]
    print(f"slug:         {row['slug']}")
    print(f"conditionId:  {row['conditionId']}")
    print(f"question:     {row.get('question', '')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Look up conditionId for a market slug")
    parser.add_argument("slug", help="Market slug from Polymarket URL")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args()
    lookup(args.slug, args.cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
