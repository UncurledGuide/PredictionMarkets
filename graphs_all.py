import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
from matplotlib.widgets import Button
import seaborn as sns
from data import annualized_df, monthly_df


# Interactive graph browser with left/right arrows.
sns.set_theme(style="whitegrid", context="talk")


def draw_scatter_vix(ax):
    sns.regplot(data=monthly_df, x="VIX_diff", y="SPX_ret", ax=ax, scatter_kws={"alpha": 0.7})
    ax.set_title("SPX Ret vs VIX Diff")
    ax.set_xlabel("VIX Monthly Diff")
    ax.set_ylabel("SPX Monthly Return")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))


def draw_scatter_epu(ax):
    sns.regplot(data=monthly_df, x="EPU_diff", y="SPX_ret", ax=ax, scatter_kws={"alpha": 0.7}, color="#9467bd")
    ax.set_title("SPX Ret vs EPU Diff")
    ax.set_xlabel("EPU Monthly Diff")
    ax.set_ylabel("SPX Monthly Return")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))


def draw_scatter_abs_epu(ax):
    sns.regplot(data=monthly_df, x="EPU_diff", y="SPX_ret_abs", ax=ax, scatter_kws={"alpha": 0.7}, color="#ff7f0e")
    ax.set_title("Abs(SPX Ret) vs EPU Diff")
    ax.set_xlabel("EPU Monthly Diff")
    ax.set_ylabel("Abs(SPX Monthly Return)")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))


def draw_line_vix(ax):
    sns.lineplot(data=monthly_df, x=monthly_df.index, y="VIX_diff", ax=ax, color="#d62728", linewidth=1.4)
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.5)
    ax.set_title("VIX Monthly Diff")
    ax.set_ylabel("Delta VIX")
    ax.set_xlabel("Date")


def draw_line_epu(ax):
    sns.lineplot(data=monthly_df, x=monthly_df.index, y="EPU_diff", ax=ax, color="#9467bd", linewidth=1.4)
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.5)
    ax.set_title("EPU Monthly Diff")
    ax.set_ylabel("Delta EPU")
    ax.set_xlabel("Date")


def draw_line_spx_ret(ax):
    sns.lineplot(data=monthly_df, x=monthly_df.index, y="SPX_ret", ax=ax, color="#2ca02c", linewidth=1.6)
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.5)
    ax.set_title("S&P 500 Monthly Returns")
    ax.set_ylabel("Return")
    ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))


def draw_line_spx_ann(ax):
    sns.lineplot(data=annualized_df, x=annualized_df.index, y="SPX_ann_return", ax=ax, color="#1f77b4", linewidth=1.8)
    ax.axhline(0, color="black", linewidth=0.8, alpha=0.5)
    ax.set_title("S&P 500 Rolling 1Y Annualized Return")
    ax.set_ylabel("Return")
    ax.set_xlabel("Date")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))


plots = [
    draw_scatter_vix,
    draw_scatter_epu,
    draw_scatter_abs_epu,
    draw_line_vix,
    draw_line_epu,
    draw_line_spx_ret,
    draw_line_spx_ann,
]

fig, ax = plt.subplots(figsize=(12, 7))
plt.subplots_adjust(bottom=0.18)
state = {"idx": 0}


def render_current():
    ax.clear()
    plots[state["idx"]](ax)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.suptitle(f"Graph {state['idx'] + 1}/{len(plots)}  (use ← / → buttons)", fontsize=13, fontweight="bold")
    fig.canvas.draw_idle()


def go_prev(_event):
    state["idx"] = (state["idx"] - 1) % len(plots)
    render_current()


def go_next(_event):
    state["idx"] = (state["idx"] + 1) % len(plots)
    render_current()


btn_prev_ax = fig.add_axes([0.38, 0.05, 0.1, 0.06])
btn_next_ax = fig.add_axes([0.52, 0.05, 0.1, 0.06])
btn_prev = Button(btn_prev_ax, "← Prev")
btn_next = Button(btn_next_ax, "Next →")
btn_prev.on_clicked(go_prev)
btn_next.on_clicked(go_next)

render_current()
plt.show()
