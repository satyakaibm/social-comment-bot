#!/usr/bin/env python3
"""Seaborn practice on the classic `tips` dataset: histplots and boxplots.

Not part of the bot. It is here as a worked example of the chart style used
on the Momentum page (fixed categorical colours, thin marks, light grid,
direct labels where they help) applied to a dataset everyone knows.

    .venv/bin/python docs/examples/seaborn_tips_plots.py --out reports/tips

`sns.load_dataset("tips")` downloads a small CSV from seaborn's GitHub
repository the first time, so the machine needs internet access once.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import seaborn as sns  # noqa: E402

# Same slots as app/growth_charts.py: colour follows the entity.
TIME_COLOR = {"Lunch": "#2a78d6", "Dinner": "#eb6834"}
SEX_COLOR = {"Male": "#2a78d6", "Female": "#1baf7a"}
SMOKER_COLOR = {"Yes": "#eb6834", "No": "#2a78d6"}
INK, INK_2, MUTED, GRID, AXIS = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
DAY_ORDER = ["Thur", "Fri", "Sat", "Sun"]


def style() -> None:
    sns.set_theme(style="whitegrid", font="sans-serif")
    plt.rcParams.update({
        "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "axes.titleweight": "bold", "axes.titlesize": 11, "axes.labelsize": 9,
        "xtick.color": MUTED, "ytick.color": MUTED, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
        "figure.titlesize": 12, "figure.titleweight": "bold",
    })


def histplots(tips, out: Path) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(14, 4))
    # 1. One variable, one hue: the shape of the bill.
    sns.histplot(data=tips, x="total_bill", bins=20, color="#2a78d6", edgecolor="white", ax=axes[0])
    axes[0].axvline(tips["total_bill"].median(), color=INK, linewidth=1)
    axes[0].annotate(f"median ${tips['total_bill'].median():.2f}", (tips["total_bill"].median(), axes[0].get_ylim()[1] * 0.9),
                     xytext=(6, 0), textcoords="offset points", fontsize=8, color=INK_2)
    axes[0].set_title("Total bill distribution")
    axes[0].set_xlabel("Total bill ($)")
    # 2. Split by a category: overlapping steps, so both shapes stay visible.
    sns.histplot(data=tips, x="total_bill", hue="time", hue_order=list(TIME_COLOR), palette=TIME_COLOR,
                 element="step", stat="density", common_norm=False, bins=20, ax=axes[1])
    axes[1].set_title("Lunch vs dinner bills")
    axes[1].set_xlabel("Total bill ($)")
    # 3. The derived number people actually care about: tip percentage.
    tips = tips.assign(tip_pct=tips["tip"] / tips["total_bill"] * 100)
    sns.histplot(data=tips, x="tip_pct", hue="sex", hue_order=list(SEX_COLOR), palette=SEX_COLOR,
                 element="step", stat="density", common_norm=False, bins=25, ax=axes[2])
    axes[2].set_xlim(0, 40)
    axes[2].set_title("Tip as % of bill, by payer")
    axes[2].set_xlabel("Tip (%)")
    fig.suptitle("Histplots: how bills and tips are distributed (seaborn tips dataset, n=244)")
    fig.tight_layout()
    path = out / "tips_histplots.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def boxplots(tips, out: Path) -> Path:
    tips = tips.assign(tip_pct=tips["tip"] / tips["total_bill"] * 100)
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.2))
    thin = {"boxprops": {"edgecolor": AXIS}, "whiskerprops": {"color": AXIS}, "capprops": {"color": AXIS},
            "medianprops": {"color": INK, "linewidth": 1.6}, "flierprops": {"marker": "o", "markersize": 3, "markerfacecolor": MUTED, "markeredgecolor": "none"}}
    # 1. One category, one hue.
    sns.boxplot(data=tips, x="day", y="total_bill", order=DAY_ORDER, color="#cde2fb", width=0.5, ax=axes[0], **thin)
    sns.stripplot(data=tips, x="day", y="total_bill", order=DAY_ORDER, color="#2a78d6", size=3, alpha=0.5, jitter=0.18, ax=axes[0])
    axes[0].set_title("Total bill by day")
    axes[0].set_xlabel("")
    axes[0].set_ylabel("Total bill ($)")
    # 2. Two categories: day x smoker.
    sns.boxplot(data=tips, x="day", y="tip_pct", hue="smoker", hue_order=list(SMOKER_COLOR), palette=SMOKER_COLOR,
                order=DAY_ORDER, width=0.6, gap=0.15, ax=axes[1], **thin)
    axes[1].set_ylim(0, 40)
    axes[1].set_title("Tip % by day and smoker")
    axes[1].set_xlabel("")
    axes[1].set_ylabel("Tip (%)")
    axes[1].legend(title="smoker", loc="upper right")
    # 3. Party size: does a bigger table tip less?
    sns.boxplot(data=tips, x="size", y="tip_pct", color="#cde2fb", width=0.5, ax=axes[2], **thin)
    counts = tips["size"].value_counts().sort_index()
    for i, (size, n) in enumerate(counts.items()):
        axes[2].text(i, 38, f"n={n}", ha="center", fontsize=8, color=MUTED)
    axes[2].set_ylim(0, 40)
    axes[2].set_title("Tip % by party size")
    axes[2].set_xlabel("People at the table")
    axes[2].set_ylabel("Tip (%)")
    fig.suptitle("Boxplots: where the middle 50% sits and what the outliers are")
    fig.tight_layout()
    path = out / "tips_boxplots.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="reports/tips")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    style()
    tips = sns.load_dataset("tips")
    for path in (histplots(tips, out), boxplots(tips, out)):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
