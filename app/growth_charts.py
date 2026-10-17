"""Growth charts and a written growth direction for the Momentum page.

Everything here is derived from the same db.* queries Momentum already runs
(video_stats_growth, engagement_activity, audience_activity,
comment_text_sample, list_audience_online) plus two small aggregate queries
of its own, so the charts never disagree with the cards above them.

Rendering uses matplotlib/seaborn on the server and returns PNG bytes; the
dashboard serves them from /momentum/charts/<name>.png and caches both the
dataset and each image through app.query_cache. pandas/matplotlib/seaborn
are imported lazily so the app still starts (with the section hidden) when
the analysis libraries are missing, and so that importing app.dashboard
never pays the matplotlib font-cache start-up cost.

Memory note: the dataset is aggregates only (per platform/day, per hour
cell, per video) -- never the raw 300k-row video_stats_history -- so a cache
entry is a few hundred KB and a render stays well inside the 512m tenant
container cap. Daily growth is computed in SQL with window functions for
the same reason.
"""
from __future__ import annotations

import io
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from typing import Any

from app import analytics, config, db

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
PLATFORMS = ("youtube", "facebook", "instagram")

# Colour follows the entity: one fixed categorical slot per platform, never
# re-assigned when a filter drops a platform (slots 1-3 of the dashboard's
# chart palette are mutually colour-blind safe). Reply states wear the
# status palette instead, so a status never impersonates a series.
PLATFORM_COLOR = {"youtube": "#2a78d6", "facebook": "#eb6834", "instagram": "#1baf7a"}
INTENT_ORDER = ("question", "request", "praise", "complaint", "other")
INTENT_COLOR = {
    "question": "#2a78d6", "request": "#eb6834", "praise": "#1baf7a",
    "complaint": "#eda100", "other": "#c3c2b7",
}
STATUS_ORDER = ("posted", "already_replied", "approved", "pending_review", "posting", "failed", "rejected")
STATUS_COLOR = {
    "posted": "#0ca30c", "already_replied": "#86b6ef", "approved": "#fab219",
    "pending_review": "#fab219", "posting": "#fab219", "failed": "#d03b3b",
    "rejected": "#c3c2b7",
}
SEQUENTIAL_STEPS = ["#f4f8fd", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#ffffff"

PENDING_STATUSES = ("pending_review", "approved", "posting")


def available() -> bool:
    """True when pandas, matplotlib and seaborn are importable."""
    return all(find_spec(name) is not None for name in ("pandas", "matplotlib", "seaborn"))


def _pd():
    import pandas as pd
    return pd


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import seaborn as sns
    sns.set_theme(style="whitegrid", font="sans-serif")
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "axes.titleweight": "bold", "axes.titlesize": 11, "axes.labelsize": 9,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelsize": 8, "ytick.labelsize": 8,
        "grid.color": GRID, "grid.linestyle": "-", "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False,
        "lines.linewidth": 2, "legend.frameon": False, "legend.fontsize": 8,
        "figure.titlesize": 12, "figure.titleweight": "bold",
    })
    return plt, sns


# ---------------------------------------------------------------------------
# Dataset: aggregates only
# ---------------------------------------------------------------------------

def _page_clause(column: str, page_key: str | None) -> tuple[str, list]:
    if not page_key:
        return "", []
    if page_key == config.DEFAULT_PAGE_KEY:
        return f" AND {column} IN (?, '')", [page_key]
    return f" AND {column} = ?", [page_key]


def _daily_growth_rows(conn, *, cutoff: str, platform: str | None, page_key: str | None) -> list[dict]:
    """Views/likes/comments gained per platform per IST day.

    Last snapshot of each video per day, differenced per video across days
    (negative steps clipped to zero like video_stats_growth), summed per
    platform. A video's first day in the window has nothing to difference
    against and is left out rather than drawn as zero. Window functions
    keep the 300k-row history inside SQLite.
    """
    clause, params = _page_clause("page_key", page_key)
    platform_clause = " AND platform = ?" if platform else ""
    sql = f"""
        WITH days AS (
            SELECT platform, video_id,
                   date(captured_at, '+5 hours', '+30 minutes') AS day,
                   view_count, like_count, comment_count,
                   ROW_NUMBER() OVER (
                       PARTITION BY platform, video_id, date(captured_at, '+5 hours', '+30 minutes')
                       ORDER BY captured_at DESC, id DESC) AS rn
              FROM video_stats_history
             WHERE captured_at >= ? {platform_clause} {clause}
        ), last_of_day AS (
            SELECT platform, video_id, day, view_count, like_count, comment_count
              FROM days WHERE rn = 1
        ), deltas AS (
            SELECT platform, day,
                   MAX(0, view_count - LAG(view_count) OVER w) AS view_growth,
                   MAX(0, like_count - LAG(like_count) OVER w) AS like_growth,
                   MAX(0, comment_count - LAG(comment_count) OVER w) AS comment_growth
              FROM last_of_day
            WINDOW w AS (PARTITION BY platform, video_id ORDER BY day)
        )
        SELECT platform, day,
               SUM(view_growth) AS view_growth,
               SUM(like_growth) AS like_growth,
               SUM(comment_growth) AS comment_growth
          FROM deltas
         WHERE view_growth IS NOT NULL OR like_growth IS NOT NULL OR comment_growth IS NOT NULL
         GROUP BY platform, day
         ORDER BY platform, day
    """
    query_params: list = [cutoff]
    if platform:
        query_params.append(platform)
    query_params.extend(params)
    return [dict(row) for row in conn.execute(sql, query_params)]


def _comment_rows(conn, *, cutoff: str, platform: str | None, page_key: str | None) -> list[dict]:
    """One row per audience comment in the window: status plus the IST day
    and reply latency we chart. Text stays out; intents come from
    comment_text_sample like Momentum's intent card."""
    clause, params = _page_clause("page_key", page_key)
    platform_clause = " AND platform = ?" if platform else ""
    published = db._published_at_utc_expr("published_at")
    stamp = (
        f"COALESCE(CASE WHEN trim(COALESCE(published_at, '')) <> '' AND {published} IS NOT NULL "
        f"THEN {published} END, created_at)"
    )
    sql = f"""
        SELECT platform, status, error IS NOT NULL AND error <> '' AS has_error,
               date({stamp}, '+5 hours', '+30 minutes') AS day,
               CASE WHEN status = 'posted'
                    THEN (julianday(updated_at) - julianday({stamp})) * 24 END AS reply_hours
          FROM comments
         WHERE created_at >= ? {platform_clause} {clause}
    """
    query_params: list = [cutoff]
    if platform:
        query_params.append(platform)
    query_params.extend(params)
    return [dict(row) for row in conn.execute(sql, query_params)]


def build_dataset(conn, *, platform: str | None, page_key: str | None, days: int) -> dict[str, Any]:
    """Everything the charts and the direction need, as small aggregates."""
    pd = _pd()
    now = datetime.now(timezone.utc)
    horizon = timedelta(days=days)
    cutoff = (now - horizon).isoformat()
    kw = {"platform": platform, "page_key": page_key}

    stats = pd.DataFrame(db.list_video_stats(conn, limit=100_000, **kw))
    growth_long = pd.DataFrame(db.video_stats_growth(conn, horizon=horizon, **kw))
    growth_7d = pd.DataFrame(db.video_stats_growth(conn, horizon=timedelta(days=7), **kw))
    engagement = pd.DataFrame(db.engagement_activity(conn, horizon=horizon, **kw))
    audience = pd.DataFrame(db.audience_activity(conn, horizon=horizon, **kw))
    intent_rows = db.comment_text_sample(conn, horizon=horizon, **kw)
    online = (
        pd.DataFrame(db.list_audience_online(conn, platform="instagram", page_key=page_key, days=28))
        if platform in (None, "", "instagram") else pd.DataFrame()
    )
    daily = pd.DataFrame(_daily_growth_rows(conn, cutoff=cutoff, **kw))
    comments = pd.DataFrame(_comment_rows(conn, cutoff=cutoff, **kw))
    experiments = db.list_recommendation_experiments(conn, limit=500, **kw)

    data: dict[str, Any] = {
        "now": now, "days": days, "platform": platform or "", "page_key": page_key or "",
        "stats": stats, "growth_long": growth_long, "growth_7d": growth_7d,
        "engagement": engagement, "audience": audience, "online": online,
        "daily": daily, "comments": comments, "experiments": experiments,
    }
    data["scoreboard"] = _scoreboard(data)
    data["movers"] = _top_movers(data)
    data["rates"] = _engagement_rates(data)
    data["intents"] = _intent_table(intent_rows)
    data["coverage"] = _reply_coverage(data)
    data["view_peaks"] = _timing_peaks(engagement, "view_growth")
    data["comment_peaks"] = _timing_peaks(audience, "comment_count")
    data["online_profile"] = _online_profile(online)
    data["comments_per_day"] = (
        comments.dropna(subset=["day"]).groupby(["platform", "day"]).size().reset_index(name="comments")
        if not comments.empty else pd.DataFrame(columns=["platform", "day", "comments"])
    )
    return data


def _scoreboard(data: dict):
    pd = _pd()
    growth, stats = data["growth_long"], data["stats"]
    rows = []
    for platform in PLATFORMS:
        g = growth[growth["platform"] == platform] if not growth.empty else growth
        s = stats[stats["platform"] == platform] if not stats.empty else stats
        if s.empty and g.empty:
            continue
        rows.append({
            "platform": platform,
            "videos_tracked": int(len(s)),
            # Facebook/Instagram items often have no view count at all; only
            # items with a measured view delta can be called stalled.
            "videos_measured": int(g["view_growth"].notna().sum()) if not g.empty else 0,
            "videos_with_view_growth": int((g["view_growth"].fillna(0) > 0).sum()) if not g.empty else 0,
            "view_growth": int(g["view_growth"].fillna(0).sum()) if not g.empty else 0,
            "like_growth": int(g["like_growth"].fillna(0).sum()) if not g.empty else 0,
            "comment_growth": int(g["comment_growth"].fillna(0).sum()) if not g.empty else 0,
            "share_growth": int(g["share_growth"].fillna(0).sum()) if not g.empty else 0,
        })
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["view_growth_per_video"] = (
            frame["view_growth"] / frame["videos_tracked"].where(frame["videos_tracked"] > 0)
        ).round(1)
    return frame


def _top_movers(data: dict, n: int = 10):
    g = data["growth_7d"]
    if g.empty:
        return g
    g = g.copy()
    g["title"] = g["video_title"].fillna(g["video_id"]).astype(str).str.slice(0, 48)
    return g.sort_values("view_growth", ascending=False).head(n).reset_index(drop=True)


def _engagement_rates(data: dict, min_views: int = 100):
    pd = _pd()
    s = data["stats"]
    if s.empty or "view_count" not in s:
        return pd.DataFrame()
    s = s[s["view_count"].fillna(0) >= min_views].copy()
    if s.empty:
        return s
    s["interactions"] = s[["like_count", "comment_count", "share_count"]].fillna(0).sum(axis=1)
    s["engagement_rate"] = s["interactions"] / s["view_count"]
    s["title"] = s["video_title"].fillna(s["video_id"]).astype(str).str.slice(0, 48)
    return s.sort_values("engagement_rate", ascending=False).reset_index(drop=True)


def _intent_table(intent_rows: list[dict]):
    pd = _pd()
    counts: dict[str, Counter] = {}
    for row in intent_rows:
        counts.setdefault(row["platform"], Counter())[analytics.classify_comment_intent(row.get("text") or "")] += 1
    rows = [
        {"platform": platform, **{k: counter.get(k, 0) for k in INTENT_ORDER}, "total": sum(counter.values())}
        for platform, counter in counts.items()
    ]
    return pd.DataFrame(rows, columns=["platform", *INTENT_ORDER, "total"])


def _reply_coverage(data: dict):
    pd = _pd()
    c = data["comments"]
    columns = ["platform", "total", "replied", "already_replied", "replied_pct", "pending", "failed", "median_reply_hours"]
    if c.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for platform, group in c.groupby("platform"):
        total = len(group)
        replied = int((group["status"] == "posted").sum())
        already = int((group["status"] == "already_replied").sum())
        latency = group["reply_hours"].dropna()
        latency = latency[latency >= 0]
        rows.append({
            "platform": platform, "total": total, "replied": replied, "already_replied": already,
            "replied_pct": round(100 * (replied + already) / total, 1) if total else 0.0,
            "pending": int(group["status"].isin(PENDING_STATUSES).sum()),
            "failed": int((group["status"] == "failed").sum()),
            "median_reply_hours": round(float(latency.median()), 1) if not latency.empty else None,
        })
    return pd.DataFrame(rows, columns=columns)


def _timing_peaks(frame, metric: str, n: int = 3):
    pd = _pd()
    if frame.empty or metric not in frame:
        return pd.DataFrame(columns=["platform", "weekday_ist", "hour_ist", metric])
    cells = frame.groupby(["platform", "weekday_ist", "hour_ist"], as_index=False)[metric].sum()
    cells = cells[cells[metric] > 0]
    return cells.sort_values(metric, ascending=False).groupby("platform").head(n).reset_index(drop=True)


def _online_profile(online):
    pd = _pd()
    if online.empty:
        return pd.DataFrame()
    o = online.copy()
    o["hour_ist"] = ((o["hour_utc"] * 60 + 330) % 1440) / 60
    return o.groupby("hour_ist")["online_count"].agg(["mean", "min", "max"]).reset_index()


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def _png(fig) -> bytes:
    plt, _ = _plt()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=144, bbox_inches="tight")
    plt.close(fig)
    return buffer.getvalue()


def _bar_labels(ax, fmt="{:,.0f}", pad=3) -> None:
    for container in ax.containers:
        ax.bar_label(container, fmt=fmt, padding=pad, fontsize=8, color=INK_2)


def chart_scoreboard(data: dict) -> bytes | None:
    board = data["scoreboard"]
    if board.empty:
        return None
    plt, _ = _plt()
    metrics = [("view_growth", "Views gained"), ("like_growth", "Likes gained"),
               ("comment_growth", "Comments gained"), ("share_growth", "Shares gained")]
    fig, axes = plt.subplots(1, 4, figsize=(13, 3.2))
    for ax, (col, title) in zip(axes, metrics):
        ax.barh(board["platform"], board[col], color=[PLATFORM_COLOR[p] for p in board["platform"]], height=0.55)
        ax.set_title(title)
        ax.invert_yaxis()
        ax.grid(axis="y", visible=False)
        _bar_labels(ax)
        ax.margins(x=0.25)
    fig.suptitle(f"Last {data['days']} days: what each platform gained across tracked content")
    fig.tight_layout()
    return _png(fig)


def chart_daily(data: dict) -> bytes | None:
    pd = _pd()
    daily, per_day = data["daily"], data["comments_per_day"]
    if daily.empty and per_day.empty:
        return None
    plt, _ = _plt()
    fig, axes = plt.subplots(2, 1, figsize=(12, 6.2), sharex=True)
    for ax, frame, column, title in (
        (axes[0], daily, "view_growth", "Views gained per day (from stats snapshots, IST days)"),
        (axes[1], per_day, "comments", "New audience comments per day"),
    ):
        for platform in PLATFORMS:
            d = frame[frame["platform"] == platform] if not frame.empty else frame
            if d.empty:
                continue
            d = d.sort_values("day")
            x = pd.to_datetime(d["day"])
            ax.plot(x, d[column], color=PLATFORM_COLOR[platform], label=platform, marker="o", markersize=3.5)
            ax.annotate(platform, (x.iloc[-1], d[column].iloc[-1]), xytext=(6, 0), textcoords="offset points",
                        fontsize=8, color=INK_2, va="center")
        ax.set_title(title)
        ax.set_ylim(bottom=0)
        if ax.has_data():
            ax.legend(loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()
    return _png(fig)


def chart_movers(data: dict) -> bytes | None:
    movers = data["movers"]
    if movers.empty or movers["view_growth"].fillna(0).sum() <= 0:
        return None
    plt, _ = _plt()
    fig, ax = plt.subplots(figsize=(10, 0.42 * len(movers) + 1.4))
    labels = [f"{t}  ({p})" for t, p in zip(movers["title"], movers["platform"])]
    ax.barh(labels, movers["view_growth"].fillna(0), color=[PLATFORM_COLOR[p] for p in movers["platform"]], height=0.6)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Views gained in the last 7 days")
    ax.set_title("Top movers: what is growing right now")
    _bar_labels(ax)
    ax.margins(x=0.15)
    fig.tight_layout()
    return _png(fig)


def _heat(ax, frame, metric: str, title: str, cbar_label: str, sns, cmap) -> None:
    pd = _pd()
    grid = pd.DataFrame(0.0, index=range(7), columns=range(24))
    if not frame.empty and metric in frame:
        pivot = frame.pivot_table(index="weekday_ist", columns="hour_ist", values=metric, aggfunc="sum", fill_value=0)
        grid.update(pivot)
    sns.heatmap(grid, ax=ax, cmap=cmap, linewidths=1, linecolor=SURFACE, cbar=True,
                cbar_kws={"label": cbar_label, "shrink": 0.8})
    ax.set_yticklabels(WEEKDAYS, rotation=0)
    ax.set_xticks([i + 0.5 for i in range(0, 24, 3)])
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 3)], rotation=0)
    ax.set_xlabel("Hour of day (IST)")
    ax.set_ylabel("")
    ax.set_title(title)


def chart_timing(data: dict) -> bytes | None:
    engagement, audience = data["engagement"], data["audience"]
    platforms = [p for p in PLATFORMS if (not engagement.empty and (engagement["platform"] == p).any())
                 or (not audience.empty and (audience["platform"] == p).any())]
    if not platforms:
        return None
    plt, sns = _plt()
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("blue_seq", SEQUENTIAL_STEPS)
    fig, axes = plt.subplots(len(platforms), 2, figsize=(14, 3.1 * len(platforms) + 0.6), squeeze=False)
    for row, platform in enumerate(platforms):
        e = engagement[engagement["platform"] == platform] if not engagement.empty else engagement
        a = audience[audience["platform"] == platform] if not audience.empty else audience
        _heat(axes[row][0], e, "view_growth", f"{platform}: when views move", "views gained", sns, cmap)
        _heat(axes[row][1], a, "comment_count", f"{platform}: when people comment", "comments", sns, cmap)
    fig.suptitle("Audience timing on the IST clock. Darker = more activity in that hour")
    fig.tight_layout()
    return _png(fig)


def chart_intents(data: dict) -> bytes | None:
    pd = _pd()
    intents = data["intents"]
    if intents.empty or intents["total"].sum() == 0:
        return None
    plt, _ = _plt()
    fig, ax = plt.subplots(figsize=(10, 0.8 * len(intents) + 1.6))
    left = pd.Series(0.0, index=intents.index)
    for intent in INTENT_ORDER:
        share = (intents[intent] / intents["total"].where(intents["total"] > 0) * 100).fillna(0).astype(float)
        bars = ax.barh(intents["platform"], share, left=left, color=INTENT_COLOR[intent], label=intent,
                       height=0.55, edgecolor=SURFACE, linewidth=2)
        for bar, value in zip(bars, share):
            if value >= 8:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_y() + bar.get_height() / 2,
                        f"{intent} {value:.0f}%", ha="center", va="center", fontsize=8, color=INK)
        left = left + share
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of comments (%)")
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.legend(ncol=5, loc="lower center", bbox_to_anchor=(0.5, 1.02))
    ax.set_title("What the audience is saying (same rule-based intent classifier as the intent cards)", pad=28)
    fig.tight_layout()
    return _png(fig)


def chart_replies(data: dict) -> bytes | None:
    pd = _pd()
    coverage, comments = data["coverage"], data["comments"]
    if coverage.empty:
        return None
    plt, _ = _plt()
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.5), gridspec_kw={"width_ratios": [3, 2]})
    ax = axes[0]
    status_counts = comments.groupby(["platform", "status"]).size().unstack(fill_value=0)
    platforms = list(status_counts.index)
    left = pd.Series(0, index=platforms)
    biggest = max(1, int(status_counts.sum(axis=1).max()))
    for status in STATUS_ORDER:
        if status not in status_counts:
            continue
        values = status_counts[status]
        bars = ax.barh(platforms, values, left=left, color=STATUS_COLOR[status], label=status.replace("_", " "),
                       height=0.55, edgecolor=SURFACE, linewidth=2)
        for bar, value in zip(bars, values):
            if value and value / biggest >= 0.06:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_y() + bar.get_height() / 2, f"{value:,}",
                        ha="center", va="center", fontsize=8, color=INK)
        left = left + values
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Comments")
    ax.set_title("Reply coverage by status", pad=26)
    ax.legend(ncol=len(STATUS_ORDER), loc="lower center", bbox_to_anchor=(0.5, 1.02), handlelength=1.2, columnspacing=1.0)
    ax = axes[1]
    lat = coverage.dropna(subset=["median_reply_hours"])
    if not lat.empty:
        ax.barh(lat["platform"], lat["median_reply_hours"], color=[PLATFORM_COLOR[p] for p in lat["platform"]], height=0.55)
        ax.invert_yaxis()
        _bar_labels(ax, fmt="{:.1f} h")
        ax.margins(x=0.3)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Hours from comment to our reply (median)")
    ax.set_title("Reply speed")
    fig.tight_layout()
    return _png(fig)


def chart_engagement_rate(data: dict) -> bytes | None:
    rates = data["rates"]
    if rates.empty:
        return None
    plt, sns = _plt()
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.2), gridspec_kw={"width_ratios": [2, 3]})
    ax = axes[0]
    order = [p for p in PLATFORMS if (rates["platform"] == p).any()]
    sns.stripplot(data=rates, x="platform", y="engagement_rate", order=order, hue="platform",
                  palette=PLATFORM_COLOR, legend=False, ax=ax, size=5, alpha=0.7, jitter=0.2)
    sns.boxplot(data=rates, x="platform", y="engagement_rate", order=order, ax=ax, width=0.35, showfliers=False,
                boxprops={"facecolor": "none", "edgecolor": AXIS}, whiskerprops={"color": AXIS},
                capprops={"color": AXIS}, medianprops={"color": INK})
    ax.set_ylabel("(likes + comments + shares) / views")
    ax.set_xlabel("")
    ax.set_title("Engagement rate per video")
    ax = axes[1]
    top = rates.head(8)
    ax.barh([f"{t}  ({p})" for t, p in zip(top["title"], top["platform"])], top["engagement_rate"] * 100,
            color=[PLATFORM_COLOR[p] for p in top["platform"]], height=0.6)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("Engagement rate (%)")
    ax.set_title("Content that converts viewers into engagement best")
    _bar_labels(ax, fmt="{:.1f}%")
    ax.margins(x=0.2)
    fig.tight_layout()
    return _png(fig)


def chart_instagram_online(data: dict) -> bytes | None:
    profile = data["online_profile"]
    if profile.empty:
        return None
    plt, _ = _plt()
    fig, ax = plt.subplots(figsize=(10, 3.4))
    color = PLATFORM_COLOR["instagram"]
    ax.fill_between(profile["hour_ist"], profile["min"], profile["max"], color=color, alpha=0.12, linewidth=0)
    ax.plot(profile["hour_ist"], profile["mean"], color=color)
    peak = profile.loc[profile["mean"].idxmax()]
    ax.scatter([peak["hour_ist"]], [peak["mean"]], color=color, s=40, zorder=3, edgecolor=SURFACE, linewidth=2)
    ax.annotate(f"peak {int(peak['hour_ist']):02d}:30 IST", (peak["hour_ist"], peak["mean"]), xytext=(8, 6),
                textcoords="offset points", fontsize=8, color=INK_2)
    ax.set_xticks(range(0, 24, 3))
    ax.set_xticklabels([f"{h:02d}:30" for h in range(0, 24, 3)])
    ax.set_xlabel("Hour (IST)")
    ax.set_ylabel("Followers online")
    ax.set_ylim(bottom=0)
    ax.set_title("Instagram followers online by hour (28-day average, band = daily min/max)")
    fig.tight_layout()
    return _png(fig)


CHARTS: dict[str, dict] = {
    "scoreboard": {"wide": True, "title": "Platform scoreboard", "render": chart_scoreboard,
                   "caption": "Views, likes, comments and shares gained in the window, summed over tracked content."},
    "daily": {"wide": True, "title": "Day by day", "render": chart_daily,
              "caption": "Views gained and new audience comments per IST day, one line per platform."},
    "movers": {"title": "Top movers (7 days)", "render": chart_movers,
               "caption": "The content gaining the most views this week. Read the titles as a set: that is what is being rewarded now."},
    "timing": {"wide": True, "title": "Audience timing", "render": chart_timing,
               "caption": "When views move and when people comment, by weekday and IST hour. Darker is busier."},
    "intents": {"title": "Comment intent", "render": chart_intents,
                "caption": "Share of questions, requests, praise and complaints per platform."},
    "replies": {"wide": True, "title": "Reply coverage and speed", "render": chart_replies,
                "caption": "How many comments got a reply, what is still pending or failed, and the median hours to reply."},
    "engagement_rate": {"wide": True, "title": "Engagement rate", "render": chart_engagement_rate,
                        "caption": "Interactions per view for every video with at least 100 views, and the eight that convert best."},
    "instagram_online": {"title": "Instagram followers online", "render": chart_instagram_online,
                         "caption": "Instagram's own online_followers insight by IST hour. The whole follower base, so the best publishing signal for reels."},
}


def render(data: dict, name: str) -> bytes | None:
    """PNG bytes for one chart, or None when the dataset has nothing to draw."""
    return CHARTS[name]["render"](data)


def chart_specs(data: dict) -> list[dict]:
    """Charts worth showing for this dataset (the ones that would not be empty)."""
    specs = []
    for name, spec in CHARTS.items():
        try:
            has_data = _has_data(data, name)
        except Exception:  # pragma: no cover - defensive, a spec check must not hide the page
            log.exception("growth chart %s availability check failed", name)
            has_data = False
        if has_data:
            specs.append({"name": name, "title": spec["title"], "caption": spec["caption"], "wide": bool(spec.get("wide"))})
    return specs


def _has_data(data: dict, name: str) -> bool:
    if name == "scoreboard":
        return not data["scoreboard"].empty
    if name == "daily":
        return not (data["daily"].empty and data["comments_per_day"].empty)
    if name == "movers":
        return not data["movers"].empty and data["movers"]["view_growth"].fillna(0).sum() > 0
    if name == "timing":
        return not (data["engagement"].empty and data["audience"].empty)
    if name == "intents":
        return not data["intents"].empty and data["intents"]["total"].sum() > 0
    if name == "replies":
        return not data["coverage"].empty
    if name == "engagement_rate":
        return not data["rates"].empty
    if name == "instagram_online":
        return not data["online_profile"].empty
    return False


# ---------------------------------------------------------------------------
# Written direction
# ---------------------------------------------------------------------------

def _hour(hour) -> str:
    return f"{int(hour):02d}:00"


def direction(data: dict) -> dict:
    """Plain-language findings and a prioritised plan, built from the dataset.

    Returns {"findings": [{"title", "points": [str, ...]}], "plan": [str, ...]}.
    Every rule is deliberately simple and auditable: thresholds are stated in
    the sentence they produce.
    """
    days = data["days"]
    board, movers, rates = data["scoreboard"], data["movers"], data["rates"]
    intents, coverage = data["intents"], data["coverage"]
    view_peaks, comment_peaks, online = data["view_peaks"], data["comment_peaks"], data["online_profile"]
    growth_all = data["growth_long"]
    findings: list[dict] = []
    plan: list[str] = []

    points: list[str] = []
    if board.empty:
        points.append("No video statistics in this window yet; the stats worker has not captured two snapshots of anything.")
    else:
        lead = board.sort_values("view_growth", ascending=False).iloc[0]
        points.append(f"{lead['platform']} gained the most views ({int(lead['view_growth']):,}). Totals are not comparable across platforms, so read each one against its own history.")
        for _, row in board.iterrows():
            measured = int(row["videos_measured"])
            if measured >= 3 and row["videos_with_view_growth"] / measured < 0.5:
                stalled = measured - int(row["videos_with_view_growth"])
                points.append(f"On {row['platform']}, {stalled} of {measured} items with view counts gained no views at all in {days} days: the back catalogue is not carrying growth, new uploads are.")
                plan.append(f"Re-surface the best of the {stalled} stalled {row['platform']} items: cut two or three into shorts or reels that link back, instead of leaving them idle.")
        total_growth = growth_all["view_growth"].fillna(0).sum() if not growth_all.empty else 0
        if total_growth > 0:
            top5 = growth_all.sort_values("view_growth", ascending=False).head(5)["view_growth"].fillna(0).sum()
            share = 100 * top5 / total_growth
            if share >= 60:
                points.append(f"The top five items captured {share:.0f}% of all view growth. Growth is hit-driven, so follow the hits while they are still being recommended.")
                plan.append("Publish a follow-up to each of this week's top three movers within seven days: same topic, same format, same hook.")
            else:
                points.append(f"The top five items captured {share:.0f}% of view growth; growth is spread across the catalogue, which is healthy. Keep the cadence steady.")
    findings.append({"title": "Where growth is coming from", "points": points})

    points = []
    if movers.empty or movers["view_growth"].fillna(0).sum() <= 0:
        points.append("No seven-day growth yet (needs at least two snapshots per item inside the week).")
    else:
        for _, row in movers.head(5).iterrows():
            points.append(f"{row['title']} ({row['platform']}): +{int(row['view_growth'] or 0):,} views, +{int(row['like_growth'] or 0):,} likes, +{int(row['comment_growth'] or 0):,} comments.")
    findings.append({"title": "What is moving this week", "points": points})

    points = []
    if rates.empty:
        points.append("Engagement rate needs view counts; YouTube always has them, Facebook and Instagram only where the API returns views.")
    else:
        median = rates["engagement_rate"].median() * 100
        points.append(f"Median engagement rate across tracked items is {median:.2f}% (interactions per view).")
        for _, row in rates.head(3).iterrows():
            points.append(f"Converts best: {row['title']} ({row['platform']}) at {row['engagement_rate'] * 100:.2f}% on {int(row['view_count']):,} views.")
        under = rates[rates["view_count"] < rates["view_count"].median()].head(3)
        if not under.empty:
            points.append("High-rate items with below-median views are the cheapest wins to promote: " + "; ".join(f"{r['title']} ({r['platform']})" for _, r in under.iterrows()) + ".")
            plan.append("Promote the top three engagement-rate items that still have below-median views: pin one, cross-post one, add one to a playlist.")
    findings.append({"title": "Content that converts viewers into fans", "points": points})

    points = []
    if view_peaks.empty and comment_peaks.empty:
        points.append("Not enough snapshot history yet to see hourly patterns.")
    else:
        for platform in PLATFORMS:
            vp = view_peaks[view_peaks["platform"] == platform] if not view_peaks.empty else view_peaks
            cp = comment_peaks[comment_peaks["platform"] == platform] if not comment_peaks.empty else comment_peaks
            if vp.empty and cp.empty:
                continue
            parts = []
            if not vp.empty:
                parts.append("views move most on " + ", ".join(f"{WEEKDAYS[int(r.weekday_ist)]} {_hour(r.hour_ist)}" for r in vp.itertuples()))
            if not cp.empty:
                parts.append("comments arrive most on " + ", ".join(f"{WEEKDAYS[int(r.weekday_ist)]} {_hour(r.hour_ist)}" for r in cp.itertuples()))
            points.append(f"{platform}: " + "; ".join(parts) + " (IST).")
            if not vp.empty:
                best = vp.iloc[0]
                plan.append(f"Schedule {platform} uploads for {WEEKDAYS[int(best['weekday_ist'])]} around {_hour((int(best['hour_ist']) - 2) % 24)} IST, two hours ahead of the strongest viewing window.")
        if not online.empty:
            peak = online.loc[online["mean"].idxmax()]
            points.append(f"Instagram followers online (Instagram's own data) peak at {int(peak['hour_ist']):02d}:30 IST with about {peak['mean']:.0f} online. That is the whole follower base, so it is the better publishing signal for reels.")
            plan.append(f"Post Instagram reels at about {(int(peak['hour_ist']) - 1) % 24:02d}:30 IST so they are an hour old when the follower-online peak arrives.")
    findings.append({"title": "When to publish and when to reply", "points": points})

    points = []
    if intents.empty or intents["total"].sum() == 0:
        points.append("No comment text in the window.")
    else:
        for _, row in intents.iterrows():
            total = row["total"] or 1
            q, r, c = row["question"], row["request"], row["complaint"]
            points.append(f"{row['platform']}: {100 * q / total:.0f}% questions, {100 * r / total:.0f}% requests, {100 * row['praise'] / total:.0f}% praise, {100 * c / total:.0f}% complaints over {int(row['total'])} comments.")
            if q / total >= 0.2:
                plan.append(f"Make one Q&A post for {row['platform']} from the recurring questions; a pinned FAQ comment answers them once and turns them into content.")
            if r / total >= 0.15:
                plan.append(f"Pick the most repeated audience request on {row['platform']} as the next topic; requests are free topic research.")
            if c / total >= 0.1:
                points.append(f"{row['platform']}: complaints are above 10%. Address them publicly before pushing more of the same content.")
    findings.append({"title": "What the audience is asking for", "points": points})

    points = []
    if coverage.empty:
        points.append("No comments in the window.")
    else:
        for _, row in coverage.iterrows():
            latency = f", median reply time {row['median_reply_hours']} h" if row["median_reply_hours"] is not None and row["median_reply_hours"] == row["median_reply_hours"] else ""
            points.append(f"{row['platform']}: {row['replied_pct']}% of {int(row['total'])} comments answered, {int(row['pending'])} pending, {int(row['failed'])} failed{latency}.")
            if row["failed"]:
                plan.append(f"Clear the {int(row['failed'])} failed {row['platform']} replies (retry or reject); a failed reply is a lost conversation.")
            if latency and row["median_reply_hours"] > 6:
                points.append(f"{row['platform']}: replies inside the first hour get seen by the commenter and by the people the platform shows the thread to; after six hours they mostly do not.")
    findings.append({"title": "Are we replying well enough?", "points": points})

    experiments = data["experiments"]
    active = sum(item.get("status") not in {"completed", "ignored"} for item in experiments)
    if not experiments:
        findings.append({"title": "Experiments", "points": ["No experiments recorded. Without a baseline you cannot tell whether a change worked."]})
        plan.append("Start one experiment from the decision queue this week so next month's charts can compare before and after.")
    else:
        findings.append({"title": "Experiments", "points": [f"{active} active of {len(experiments)} recorded."]})

    if not plan:
        plan.append("Keep publishing on the current cadence and check back in a week once more snapshots exist.")
    deduped = list(dict.fromkeys(plan))
    return {"findings": findings, "plan": deduped}


def direction_markdown(data: dict, direction_result: dict, chart_names: list[str]) -> str:
    """The direction as a Markdown document, for the CLI report."""
    scope = ", ".join(filter(None, [data["platform"] or "all platforms", data["page_key"]]))
    lines = [f"# Social growth direction ({scope}, last {data['days']} days)", "",
             f"Generated {data['now'].astimezone(IST):%Y-%m-%d %H:%M} IST from the same queries as the Momentum page. "
             "Stats history is capped at 30 days by the YouTube retention policy, so nothing here looks further back.", ""]
    for index, section in enumerate(direction_result["findings"], 1):
        lines += [f"## {index}. {section['title']}", ""]
        lines += [f"- {point}" for point in section["points"]]
        lines.append("")
    lines += ["## Your plan for the next 30 days", ""]
    lines += [f"{i}. {step}" for i, step in enumerate(direction_result["plan"], 1)]
    lines += ["", "## Charts", ""]
    lines += [f"- `{name}.png`" for name in chart_names]
    lines.append("")
    return "\n".join(lines)
