"""
Pull and graph historical price (= probability) data from Polymarket.

Polymarket outcome tokens settle at $1 if the outcome occurs and $0 otherwise,
so the traded price of a YES/NO token is the market-implied probability.

APIs used (no auth required):
  - Gamma:  https://gamma-api.polymarket.com   (market & event metadata)
  - CLOB:   https://clob.polymarket.com         (historical prices)

Closed / resolved markets:
  - Prefer Gamma path URLs `/markets/slug/{slug}` and `/events/slug/{slug}` — the list
    endpoint `?slug=` often returns [] for old markets.
  - `prices-history` expects the outcome token id from `clobTokenIds`, not `conditionId`.
  - Wide `--start`/`--end` ranges are fetched in ~13-day chunks (CLOB rejects longer windows).

Usage examples:
  python polymarket_history.py --event-slug fed-decision-in-october
  python polymarket_history.py --market-slug will-bitcoin-hit-150k-by-dec-31
  python polymarket_history.py --token-id 71321045679252212594626385532706912750332728571942...
  python polymarket_history.py --event-slug presidential-election-winner-2028 \
      --interval 1d --save-csv probs.csv
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import matplotlib.pyplot as plt
import pandas as pd
import requests
import seaborn as sns


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
INTERVALS = ("1h", "6h", "1d", "max", "all")
# CLOB rejects long startTs/endTs windows ("interval is too long"). Chunk without `interval`.
CHUNK_SECONDS = 13 * 86400

# Edit this block if you prefer "run file" over CLI flags.
USER_CONFIG = {
    "use_file_config": True,
    # Choose exactly one source (set the others to None / []):
    "event_slug": None,
    "market_slug": "will-donald-trump-win-the-2024-us-presidential-election",
    "token_id": [],  # example: ["123...", "456..."]
    "label": [],  # labels for token_id, same order
    # History window:
    "interval": "max",  # one of: 1h, 6h, 1d, max, all (ignored when start/end are set)
    "fidelity": 60,  # optional: minutes per sample (works per chunk when using start/end)
    "start": "2024-01-04",
    "end": "2025-01-31",
    # Output:
    "save_csv": None,
    "save_png": None,
    "no_show": False,
}


def _get(url, params=None):
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _parse_json_field(value):
    # Gamma returns clobTokenIds / outcomes as JSON-encoded strings.
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return [value]
    return value or []


def fetch_event(slug):
    # Path slug works for closed events; list ?slug= sometimes returns [].
    try:
        return _get(f"{GAMMA_BASE}/events/slug/{slug}")
    except requests.HTTPError:
        data = _get(f"{GAMMA_BASE}/events", params={"slug": slug})
        if isinstance(data, list):
            if not data:
                raise ValueError(f"No event found for slug '{slug}'") from None
            return data[0]
        return data


def fetch_market_by_slug(slug):
    try:
        return _get(f"{GAMMA_BASE}/markets/slug/{slug}")
    except requests.HTTPError:
        data = _get(f"{GAMMA_BASE}/markets", params={"slug": slug})
        if isinstance(data, list):
            if not data:
                raise ValueError(
                    f"No market found for slug '{slug}'. "
                    "Closed markets: use the exact slug from the Polymarket URL, or try /markets/slug/ in a browser."
                ) from None
            return data[0]
        return data


def markets_from_event(event):
    return event.get("markets") or []


def outcomes_from_market(market):
    token_ids = _parse_json_field(market.get("clobTokenIds"))
    outcome_names = _parse_json_field(market.get("outcomes"))
    if not token_ids:
        raise ValueError(
            f"Market '{market.get('slug') or market.get('id')}' has no CLOB token ids "
            "(it may be inactive or non-tradable)."
        )
    while len(outcome_names) < len(token_ids):
        outcome_names.append(f"Outcome {len(outcome_names) + 1}")
    return list(zip(outcome_names, token_ids))


def _prices_history_to_df(data):
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        raise ValueError(err)
    rows = data.get("history", []) if isinstance(data, dict) else []
    if not rows:
        return pd.DataFrame(columns=["timestamp", "price"])
    df = pd.DataFrame(rows).rename(columns={"t": "timestamp", "p": "price"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    return df.set_index("timestamp").sort_index()


def _fetch_price_history_once(token_id, interval=None, fidelity=None, start_ts=None, end_ts=None):
    params = {"market": token_id}
    if interval:
        params["interval"] = interval
    if fidelity is not None:
        params["fidelity"] = fidelity
    if start_ts is not None:
        params["startTs"] = start_ts
    if end_ts is not None:
        params["endTs"] = end_ts
    data = _get(f"{CLOB_BASE}/prices-history", params=params)
    return _prices_history_to_df(data)


def fetch_price_history(token_id, interval=None, fidelity=None, start_ts=None, end_ts=None):
    """
    Pull CLOB price history. The `market` query param is the outcome token id from Gamma's
    clobTokenIds (not conditionId).

    When start_ts and end_ts span more than ~15 days, the API returns an error unless we
    omit `interval` and request shorter chunks.
    """
    if start_ts is not None and end_ts is not None and end_ts > start_ts:
        if (end_ts - start_ts) > CHUNK_SECONDS:
            frames = []
            cur = int(start_ts)
            end_i = int(end_ts)
            while cur < end_i:
                nxt = min(cur + CHUNK_SECONDS, end_i)
                try:
                    chunk = _fetch_price_history_once(
                        token_id, interval=None, fidelity=fidelity, start_ts=cur, end_ts=nxt
                    )
                except ValueError as err:
                    print(f"  chunk {cur}–{nxt}: {err}", file=sys.stderr)
                    chunk = pd.DataFrame(columns=["timestamp", "price"])
                if not chunk.empty:
                    frames.append(chunk)
                cur = nxt
            if not frames:
                return pd.DataFrame(columns=["timestamp", "price"])
            out = pd.concat(frames).sort_index()
            out = out[~out.index.duplicated(keep="first")]
            return out
    return _fetch_price_history_once(
        token_id, interval=interval, fidelity=fidelity, start_ts=start_ts, end_ts=end_ts
    )


def build_probability_frame(label_token_pairs, **history_kwargs):
    series = {}
    empty = []
    for label, token_id in label_token_pairs:
        hist = fetch_price_history(token_id, **history_kwargs)
        if hist.empty:
            empty.append(label)
            continue
        series[label] = hist["price"]
        print(f"  pulled {len(hist):>5} points for '{label}'")
    if empty:
        print(
            f"  (no history for {len(empty)} outcome(s): {', '.join(empty)} — "
            "try a narrower --start/--end, or confirm the market slug via Gamma /markets/slug/…)",
            file=sys.stderr,
        )
    if not series:
        return pd.DataFrame()
    return pd.concat(series, axis=1).sort_index().ffill()


def parse_iso(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def plot_probabilities(prob_df, title):
    sns.set_theme(style="whitegrid", context="talk")
    fig, ax = plt.subplots(figsize=(13, 7))
    for col in prob_df.columns:
        ax.plot(prob_df.index, prob_df[col], linewidth=1.7, label=col)
    ax.set_ylim(0, 1)
    ax.set_ylabel("Implied probability")
    ax.set_xlabel("Date (UTC)")
    ax.set_title(title, fontsize=15, fontweight="bold")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y * 100:.0f}%"))
    ax.legend(loc="best", frameon=True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    return fig


def collect_targets(args):
    # Returns (title, [(label, token_id), ...])
    if args.token_id:
        labels = args.label or [f"Token {tid[:6]}…" for tid in args.token_id]
        if len(labels) != len(args.token_id):
            raise ValueError("--label count must match --token-id count")
        return "Polymarket token history", list(zip(labels, args.token_id))

    if args.market_slug:
        market = fetch_market_by_slug(args.market_slug)
        title = market.get("question") or market.get("slug") or args.market_slug
        return title, outcomes_from_market(market)

    if args.event_slug:
        event = fetch_event(args.event_slug)
        title = event.get("title") or event.get("slug") or args.event_slug
        pairs = []
        for market in markets_from_event(event):
            try:
                market_outcomes = outcomes_from_market(market)
            except ValueError as err:
                print(f"  skipping market: {err}", file=sys.stderr)
                continue
            # For multi-outcome events, use the market question (= the YES outcome).
            question = market.get("question") or market.get("groupItemTitle") or market.get("slug")
            if len(market_outcomes) == 2 and question:
                yes_label = market.get("groupItemTitle") or question
                pairs.append((yes_label, market_outcomes[0][1]))
            else:
                for outcome_name, tid in market_outcomes:
                    pairs.append((f"{question} — {outcome_name}", tid))
        if not pairs:
            raise ValueError(f"Event '{args.event_slug}' has no tradable markets.")
        return title, pairs

    raise ValueError("Provide one of --event-slug, --market-slug, or --token-id")


def make_args_from_config(config):
    tokens = config.get("token_id") or []
    market_slug = config.get("market_slug")
    event_slug = config.get("event_slug")
    sources = sum([bool(tokens), bool(market_slug), bool(event_slug)])
    if sources != 1:
        raise ValueError(
            "USER_CONFIG: set exactly one of event_slug, market_slug, or token_id "
            f"(got {sources} non-empty source fields)."
        )
    return argparse.Namespace(
        event_slug=event_slug,
        market_slug=market_slug,
        token_id=tokens,
        label=config.get("label") or [],
        interval=config.get("interval", "max"),
        fidelity=config.get("fidelity"),
        start=config.get("start"),
        end=config.get("end"),
        save_csv=config.get("save_csv"),
        no_show=bool(config.get("no_show", False)),
        save_png=config.get("save_png"),
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--event-slug", help="Polymarket event slug, e.g. fed-decision-in-october")
    src.add_argument("--market-slug", help="Polymarket market slug")
    src.add_argument("--token-id", action="append", help="CLOB token id (repeatable)")
    parser.add_argument("--label", action="append", help="Label for each --token-id (repeatable, same order)")
    parser.add_argument("--interval", choices=INTERVALS, default="max",
                        help="Time interval window the API returns. Default: max")
    parser.add_argument("--fidelity", type=int, help="Sample resolution in minutes (e.g. 60 for hourly)")
    parser.add_argument("--start", help="ISO datetime, e.g. 2024-01-01 (overrides --interval)")
    parser.add_argument("--end", help="ISO datetime end (overrides --interval)")
    parser.add_argument("--save-csv", help="Write the assembled probability dataframe to this CSV path")
    parser.add_argument("--no-show", action="store_true", help="Don't open the matplotlib window")
    parser.add_argument("--save-png", help="Save the chart to this PNG path")
    return parser


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv and USER_CONFIG.get("use_file_config", False):
        args = make_args_from_config(USER_CONFIG)
    else:
        parser = build_parser()
        args = parser.parse_args(argv)

    title, pairs = collect_targets(args)
    print(f"Pulling history for {len(pairs)} outcome(s):")
    for label, _ in pairs:
        print(f"  • {label}")

    history_kwargs = {"fidelity": args.fidelity}
    if args.start or args.end:
        # Long ranges require chunked requests *without* `interval` (CLOB rejects them otherwise).
        history_kwargs["start_ts"] = parse_iso(args.start)
        history_kwargs["end_ts"] = parse_iso(args.end)
    else:
        history_kwargs["interval"] = args.interval

    prob_df = build_probability_frame(pairs, **history_kwargs)
    if prob_df.empty:
        print("No price history returned for any outcome.", file=sys.stderr)
        return 1

    print(f"\nProbability frame: {len(prob_df)} rows x {len(prob_df.columns)} cols")
    print(prob_df.tail(5).round(3).to_string())

    if args.save_csv:
        prob_df.to_csv(args.save_csv)
        print(f"Wrote {args.save_csv}")

    fig = plot_probabilities(prob_df, title)
    if args.save_png:
        fig.savefig(args.save_png, dpi=150, bbox_inches="tight")
        print(f"Wrote {args.save_png}")
    if not args.no_show:
        plt.show()
    return 0


if __name__ == "__main__":
    sys.exit(main(None))
