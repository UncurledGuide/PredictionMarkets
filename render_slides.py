#!/usr/bin/env python3
"""Render the 3 geopolitics slides as PNGs, matching the Pop Culture Canva format."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

NAVY      = "#0b1a3a"   # dark panel background
NAVY_BOX  = "#1c3563"   # lighter narrative box
WHITE     = "#ffffff"
INK       = "#1a1a1a"   # near-black table text
GRID      = "#d9d9d9"   # thin gray gridlines
HEADER    = "#111111"

W, H = 1920, 1080
DPI = 160

def newfig():
    fig = plt.figure(figsize=(W/DPI, H/DPI), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    ax.axis("off")
    return fig, ax

# ---------------------------------------------------------------- LAYOUT B
def layout_b(path, header, rows):
    """Full-width 4-col comparison table. rows = list of (label, informed, uninformed, stat).
    A label may carry a trailing '*' marker via dict {'bold_stat': True}."""
    fig, ax = newfig()
    ax.add_patch(Rectangle((0, 0), 1, 1, color=WHITE))
    cols_x = [0.015, 0.385, 0.565, 0.715]   # left edges of the 4 columns
    headers = [header, "Informed", "Uninformed", "Statistical Test / Difference"]
    n = len(rows) + 1
    top, bot = 0.97, 0.03
    row_h = (top - bot) / n
    def y(i): return top - i * row_h - row_h * 0.5
    # header row
    ax.text(cols_x[0], y(0), headers[0], fontsize=14, fontweight="bold", color=HEADER, va="center", ha="left")
    ax.text(cols_x[1], y(0), headers[1], fontsize=14, fontweight="bold", color=HEADER, va="center", ha="left")
    ax.text(cols_x[2], y(0), headers[2], fontsize=14, fontweight="bold", color=HEADER, va="center", ha="left")
    ax.text(cols_x[3], y(0), headers[3], fontsize=12.5, fontweight="bold", color=HEADER, va="center", ha="left")
    ax.plot([0.012, 0.988], [top - row_h, top - row_h], color="#333333", lw=1.4)
    # data rows
    for i, (label, inf, unf, stat) in enumerate(rows, start=1):
        yy = y(i)
        ax.text(cols_x[0], yy, label, fontsize=12.5, fontweight="bold", color=INK, va="center", ha="left")
        ax.text(cols_x[1], yy, inf,   fontsize=12.5, color=INK, va="center", ha="left")
        ax.text(cols_x[2], yy, unf,   fontsize=12.5, color=INK, va="center", ha="left")
        bold = stat.startswith("**")
        s = stat.strip("*")
        ax.text(cols_x[3], yy, s, fontsize=11.5, color=INK, va="center", ha="left",
                fontweight="bold" if bold else "normal")
        ax.plot([0.012, 0.988], [top - (i+1)*row_h, top - (i+1)*row_h], color=GRID, lw=0.8)
    fig.savefig(path, dpi=DPI); plt.close(fig)
    print("wrote", path)

# ---------------------------------------------------------------- LAYOUT A
def layout_a(path, table_rows, panel_title, panel_body):
    """Split: left white Metric|Result table, right navy narrative panel."""
    fig, ax = newfig()
    ax.add_patch(Rectangle((0, 0), 1, 1, color=WHITE))
    split = 0.50
    # right navy panel
    ax.add_patch(Rectangle((split, 0), 1 - split, 1, color=NAVY))
    # ---- left table
    top, bot = 0.96, 0.04
    n = len(table_rows) + 1
    row_h = (top - bot) / n
    def y(i): return top - i * row_h - row_h * 0.5
    lx, rx = 0.02, 0.225
    ax.text(lx, y(0), "Metric", fontsize=14, fontweight="bold", color=HEADER, va="center")
    ax.text(rx, y(0), "Result", fontsize=14, fontweight="bold", color=HEADER, va="center")
    ax.plot([0.015, split - 0.02], [top - row_h, top - row_h], color="#333333", lw=1.2)
    for i, (k, v) in enumerate(table_rows, start=1):
        yy = y(i)
        # wrap long values
        ax.text(lx, yy, k, fontsize=10.5, color=INK, va="center", ha="left")
        ax.text(rx, yy, v, fontsize=10.5, color=INK, va="center", ha="left", wrap=True)
        ax.plot([0.015, split - 0.02], [top - (i+1)*row_h, top - (i+1)*row_h], color=GRID, lw=0.7)
    # ---- right panel title
    ax.text(split + (1 - split)/2, 0.86, panel_title, fontsize=40, fontweight="bold",
            color=WHITE, va="center", ha="center", family="sans-serif")
    # narrative box
    bx0, bx1 = split + 0.035, 0.972
    by0, by1 = 0.10, 0.72
    ax.add_patch(Rectangle((bx0, by0), bx1 - bx0, by1 - by0, color=NAVY_BOX))
    # body text centered in box
    cx = (bx0 + bx1) / 2
    ax.text(cx, (by0 + by1)/2, panel_body, fontsize=17, color=WHITE,
            va="center", ha="center", linespacing=1.5, wrap=True)
    fig.savefig(path, dpi=DPI); plt.close(fig)
    print("wrote", path)

# ================================================================ SLIDE 1
slide1_rows = [
    ("Cohort Size", "40 wallets", "—", "—"),
    ("Total Trades", "2,443", "180,374", "—"),
    ("Mean Directional Edge", "-46.88¢", "-53.04¢", "**+6.16¢"),
    ("25th Percentile Edge (p25)", "-97.90¢", "-97.50¢", "—"),
    ("Median Edge (p50)", "-85.00¢", "-85.00¢", "—"),
    ("75th Percentile Edge (p75)", "-0.20¢", "-0.70¢", "—"),
    ("Welch t-test", "—", "—", "**t = +4.769,  p < 0.001"),
    ("Informed vs. Zero Edge", "—", "—", "t = -36.479,  p < 0.001"),
    ("Winning Trades (n)", "531", "25,462", "—"),
    ("Mean Entry Price (winners)", "46.4¢", "61.8¢", "**Gap = +15.33¢"),
    ("Median Entry Price (winners)", "49.0¢", "67.6¢", "—"),
    ("Entry Price Gap Test", "—", "—", "**t = -9.935,  p < 0.001"),
    ("Timing Median (days before res.)", "10.5 d", "12.1 d", "—"),
    ("Mean Days Before Resolution", "23.3 d", "22.1 d", "—"),
    ("Trade Earlier?", "slightly", "—", "t = +1.412,  p = 0.158 (ns)"),
]
layout_b("results/slide1_predictive_power.png", "PREDICTIVE POWER — GEOPOLITICS", slide1_rows)

# ================================================================ SLIDE 2
slide2_table = [
    ("Informed Cohort", "40 wallets"),
    ("Total Trades Analyzed", "182,817"),
    ("Informed Edge", "-46.88¢"),
    ("Uninformed Edge", "-53.04¢"),
    ("Edge Difference", "+6.16¢"),
    ("Difference Significant?", "Yes (p < 0.001)"),
    ("Informed Profitable Overall?", "No — negative edge,\nbut beats the baseline"),
    ("Enter Earlier?", "No — not significant (p = 0.16)"),
    ("Winning Trade Entry Price", "46.4¢ vs 61.8¢"),
    ("Entry Price Advantage", "15.33¢ cheaper for\ninformed traders"),
    ("Overall Conclusion", "Informed traders entered\nwinning outcomes far cheaper\nand beat the uninformed\nbaseline significantly — though\nboth lost on raw per-trade edge."),
]
slide2_body = ("Informed geopolitics traders are\ndistinguished primarily by entry price,\n"
               "buying winning outcomes at 46¢ on\naverage vs 62¢ for the crowd.\n\n"
               "Unlike other verticals, here informed\nwallets also beat the uninformed\n"
               "baseline directionally (+6.16¢,\np < 0.001) — concentrated in\n"
               "US-strikes-Iran and ceasefire markets,\nwhere private timing pays off.")
layout_a("results/slide2_geopolitics.png", slide2_table, "GEOPOLITICS", slide2_body)

# ================================================================ SLIDE 3
slide3_table = [
    ("Wallets Profiled (funding pulled)", "24 of 40 cohort"),
    ("Funded & Traded Within 24h", "6 wallets"),
    ("Of Those, Profitable", "6 of 6"),
    ("Fastest Funding Latency", "0.0h (same hour as\nfirst trade)"),
    ("Largest Single-Wallet P&L", "+$424,802"),
    ("Standout Fast-Funder", "0x92a6294c — $1,499 in,\ntraded same hour, +$210,426"),
    ("Cleanest Predictive Pattern", "0xd48a81db — all 60 winning\nmarkets entered below 85¢"),
    ("False Positive Removed", "0x54b56146 — market maker\n(3,937 trades, both sides)"),
    ("Dominant Markets", "US-strikes-Iran-by-[date],\nUS–Iran & Russia–Ukraine\nceasefires"),
]
slide3_body = ("Six wallets deposited fresh capital and\nplaced large directional bets within\n"
               "24 hours — three within the same hour.\nThey entered eventual winners cheap\n"
               "(often below 25¢) and cashed out\nat resolution.\n\n"
               "Two related wallets cleared over $400K\non US-strikes-Iran timing markets.\n"
               "We removed one false positive — a\nmarket maker quoting both sides.")
layout_a("results/slide3_funding.png", slide3_table, "SMOKING GUN", slide3_body)

print("\nAll slides rendered.")
