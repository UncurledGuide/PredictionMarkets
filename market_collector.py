"""Resolved Polymarket market slug collector.

Pulls closed markets from Gamma /markets, caches to parquet, filters by
volume / date / binary outcome / keywords.

Usage:
    python market_collector.py --keywords fomc,powell --refresh-cache
    python market_collector.py --keywords "fed rate" --min-volume 100000
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
DEFAULT_CACHE = Path("data/all_resolved_markets.parquet")


def safe_float(x) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0
    s = str(x).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def parse_json_field(x):
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(parsed, list):
            return parsed
        return [parsed]
    return []


def parse_iso_date(s):
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    text = str(s).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def fetch_markets_to_cache(
    cache_path: Path,
    *,
    refresh: bool,
    min_volume_usd: float,
    page_limit: int = 500,
    hard_cap: int = 20_000,
) -> list[dict]:
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists() and not refresh:
        cached_df = pd.read_parquet(cache_path)
        markets = cached_df.to_dict(orient="records")
        print(f"Loaded {len(markets)} markets from cache")
        return markets

    markets: list[dict] = []
    offset = 0
    page_count = 0
    early_stop_threshold = float(min_volume_usd) / 10.0
    headers = {"User-Agent": "Mozilla/5.0 (research-project)"}

    while len(markets) < hard_cap:
        params = {
            "closed": "true",
            "limit": page_limit,
            "offset": offset,
            "order": "volumeNum",
            "ascending": "false",
        }
        page = None
        for attempt in range(1, 4):
            try:
                resp = requests.get(GAMMA_MARKETS_URL, params=params, headers=headers, timeout=30)
                resp.raise_for_status()
                page = resp.json()
                break
            except (requests.exceptions.RequestException, json.JSONDecodeError, ValueError) as err:
                if attempt == 3:
                    print(f"  request failed after 3 attempts at offset {offset}: {err}")
                    page = []
                    break
                sleep_for = 2 ** (attempt - 1)
                print(f"  retry attempt {attempt} after error: {err} (sleeping {sleep_for}s)")
                time.sleep(sleep_for)

        if not isinstance(page, list) or not page:
            break

        markets.extend(page)
        page_count += 1
        if page_count % 5 == 0:
            print(f"  page {page_count}: offset={offset}, accumulated={len(markets)}")

        if len(page) < page_limit:
            break

        last_volume = safe_float(page[-1].get("volumeNum"))
        if last_volume < early_stop_threshold:
            print(f"  early stop: last volumeNum={last_volume:,.0f} < {early_stop_threshold:,.0f}")
            break

        offset += page_limit

    markets = markets[:hard_cap]
    if markets:
        df_to_cache = pd.DataFrame(markets)
        df_to_cache = df_to_cache.astype(
            {c: "string" for c in df_to_cache.select_dtypes("object").columns}
        )
        df_to_cache.to_parquet(cache_path, index=False)
        print(f"Fetched {len(markets)} markets, saved to {cache_path}")
    else:
        print("Fetched 0 markets — nothing saved to cache")
    return markets


def collect_resolved_market_slugs(
    keywords: list[str],
    min_volume_usd: float = 100_000,
    min_resolution_date: str = "2023-01-01",
    max_resolution_date: str | None = None,
    require_binary: bool = True,
    cache_path: str | Path = DEFAULT_CACHE,
    refresh_cache: bool = False,
) -> pd.DataFrame:
    cache_file = Path(cache_path)
    markets = fetch_markets_to_cache(
        cache_file, refresh=refresh_cache, min_volume_usd=min_volume_usd,
    )
    total_markets = len(markets)

    min_dt = parse_iso_date(min_resolution_date) or datetime.min
    max_dt = parse_iso_date(max_resolution_date) if max_resolution_date else datetime.now(UTC).replace(tzinfo=None)

    after_volume = [m for m in markets if safe_float(m.get("volumeNum")) >= float(min_volume_usd)]

    after_date = []
    bad_date_count = 0
    for m in after_volume:
        dt = parse_iso_date(m.get("endDate"))
        if dt is None:
            bad_date_count += 1
            continue
        dt_naive = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
        if min_dt <= dt_naive <= max_dt:
            m["_end_date_parsed"] = dt_naive
            after_date.append(m)

    after_binary = []
    if require_binary:
        for m in after_date:
            prices = parse_json_field(m.get("outcomePrices"))
            if len(prices) != 2:
                continue
            try:
                floats = [float(p) for p in prices]
            except (TypeError, ValueError):
                continue
            if sorted(floats) == [0.0, 1.0]:
                m["_resolution_outcome"] = "YES" if floats[0] == 1.0 else "NO"
                after_binary.append(m)
    else:
        for m in after_date:
            prices = parse_json_field(m.get("outcomePrices"))
            outcome = None
            if len(prices) == 2:
                try:
                    floats = [float(p) for p in prices]
                    if sorted(floats) == [0.0, 1.0]:
                        outcome = "YES" if floats[0] == 1.0 else "NO"
                except (TypeError, ValueError):
                    pass
            m["_resolution_outcome"] = outcome
            after_binary.append(m)

    norm_keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]

    def keyword_hits(market):
        text_parts = [
            market.get("question") or "",
            market.get("slug") or "",
            market.get("description") or "",
        ]
        text_lower = " ".join(str(p) for p in text_parts).lower()
        hits = []
        for kw in norm_keywords:
            kw_lower = kw.lower()
            if " " not in kw and len(kw) <= 4:
                pattern = r"\b" + re.escape(kw_lower) + r"\b"
                if re.search(pattern, text_lower):
                    hits.append(kw)
            elif kw_lower in text_lower:
                hits.append(kw)
        return hits

    matched_rows = []
    per_keyword_counts = {kw: 0 for kw in norm_keywords}
    for m in after_binary:
        hits = keyword_hits(m) if norm_keywords else []
        if norm_keywords and not hits:
            continue
        for h in hits:
            per_keyword_counts[h] = per_keyword_counts.get(h, 0) + 1

        events = m.get("events")
        event_slug = None
        if isinstance(events, list) and events:
            first = events[0]
            if isinstance(first, dict):
                event_slug = first.get("slug")
        elif isinstance(events, str):
            try:
                parsed_events = json.loads(events)
                if isinstance(parsed_events, list) and parsed_events:
                    first = parsed_events[0]
                    if isinstance(first, dict):
                        event_slug = first.get("slug")
            except (json.JSONDecodeError, ValueError):
                pass

        desc = m.get("description")
        desc_trunc = (str(desc)[:200]) if desc else None

        matched_rows.append({
            "market_slug": m.get("slug"),
            "question": m.get("question"),
            "volume_usd": safe_float(m.get("volumeNum")),
            "end_date": m.get("_end_date_parsed"),
            "resolution_outcome": m.get("_resolution_outcome"),
            "condition_id": m.get("conditionId"),
            "clob_token_ids": parse_json_field(m.get("clobTokenIds")),
            "event_slug": event_slug,
            "category": m.get("category") if m.get("category") else None,
            "description": desc_trunc,
            "matched_keywords": hits,
        })

    result = pd.DataFrame(
        matched_rows,
        columns=[
            "market_slug", "question", "volume_usd", "end_date",
            "resolution_outcome", "condition_id", "clob_token_ids",
            "event_slug", "category", "description", "matched_keywords",
        ],
    )
    if not result.empty:
        result = result.sort_values("volume_usd", ascending=False).reset_index(drop=True)

    print("\n=== Collection Summary ===")
    print(f"Total markets in cache: {total_markets}")
    print(f"After volume filter (>=${float(min_volume_usd):,.0f}): {len(after_volume)}")
    date_label_end = max_resolution_date if max_resolution_date else max_dt.strftime("%Y-%m-%d")
    print(f"After date filter [{min_resolution_date}, {date_label_end}]: {len(after_date)}")
    print(f"Dropped (unparseable date): {bad_date_count}")
    if require_binary:
        print(f"After binary filter: {len(after_binary)}")
    print(f"Final matched count: {len(result)}")

    if norm_keywords:
        print("\nPer-keyword match counts:")
        max_kw_len = max(len(kw) for kw in norm_keywords)
        for kw in norm_keywords:
            print(f'  "{kw}":{" " * (max_kw_len - len(kw))}  {per_keyword_counts.get(kw, 0)} markets')

    if result.empty:
        print("\nWarning: zero markets matched. Try broadening keywords or filters.")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect resolved Polymarket markets by keyword")
    parser.add_argument(
        "--keywords", required=True,
        help="Comma-separated keywords (e.g. fomc,powell,fed rate)",
    )
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="Parquet cache path")
    parser.add_argument("--refresh-cache", action="store_true", help="Re-fetch from Gamma API")
    parser.add_argument("--min-volume", type=float, default=100_000)
    parser.add_argument("--min-date", default="2023-01-01")
    parser.add_argument("--max-date", default=None)
    parser.add_argument("--no-binary", action="store_true", help="Don't require binary outcomes")
    parser.add_argument("-o", "--output", help="Optional CSV output for matched markets")
    args = parser.parse_args()

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    result = collect_resolved_market_slugs(
        keywords=keywords,
        min_volume_usd=args.min_volume,
        min_resolution_date=args.min_date,
        max_resolution_date=args.max_date,
        require_binary=not args.no_binary,
        cache_path=args.cache,
        refresh_cache=args.refresh_cache,
    )

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(out, index=False)
        print(f"\nWrote {len(result)} rows to {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
