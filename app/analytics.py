from collections import defaultdict
import hashlib
import re


WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
INTENT_WEIGHTS = {
    "question": 1.6,
    "request": 1.4,
    "praise": 1.2,
    "complaint": 0.7,
    "other": 0.3,
}
TIMING_SIGNAL_WEIGHTS = {
    "demand": 0.30,
    "quality": 0.25,
    "engagement": 0.45,
}


def creator_focus(rows: list[dict], *, limit: int = 4) -> list[dict]:
    """Build transparent early recommendations from the latest cached stats.

    Scores are deliberately calculated only within each channel/platform group.
    Cross-platform raw totals are not comparable because each API exposes
    different metrics and each channel has a different audience size.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row.get("page_key") or "", row["platform"])].append(row)

    recommendations = []
    for (page_key, platform), content in groups.items():
        ranked = sorted(content, key=_engagement_score, reverse=True)
        leader = ranked[0]
        sample_size = len(content)
        confidence = "high" if sample_size >= 10 else "medium" if sample_size >= 3 else "early"
        available_metrics = sum(
            value is not None
            for row in content
            for value in (
                row.get("view_count"), row.get("like_count"),
                row.get("comment_count"), row.get("share_count"),
            )
        )
        has_signal = available_metrics > 0
        recommendations.append(
            {
                "page_key": page_key,
                "page_label": leader.get("page_label") or "Default channel",
                "platform": platform,
                "video_id": leader["video_id"],
                "title": leader.get("video_title") or leader["video_id"],
                "content_count": sample_size,
                "confidence": confidence,
                "score": _engagement_score(leader),
                "message": (
                    f"Build on “{leader.get('video_title') or leader['video_id']}”; "
                    "it has the strongest current engagement signal in this group."
                    if has_signal
                    else "Keep collecting engagement data before choosing a content focus."
                ),
                "has_signal": has_signal,
            }
        )

    # Card order is presentational, not a cross-platform ranking.
    return sorted(
        recommendations,
        key=lambda item: (item["page_label"].casefold(), item["platform"]),
    )[:limit]


def _engagement_score(row: dict) -> float:
    """A readable baseline score; history-based momentum will supersede it."""
    likes = row.get("like_count") or 0
    comments = row.get("comment_count") or 0
    shares = row.get("share_count") or 0
    views = row.get("view_count")
    interactions = likes + (2 * comments) + (3 * shares)
    if views and views > 0:
        return (interactions / views) * 100
    return float(interactions)


def momentum_focus(rows: list[dict], *, period_label: str, limit: int = 4) -> list[dict]:
    """Rank growth within each channel/platform using percentile components."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row.get("page_key") or "", row["platform"])].append(row)

    results = []
    for (_page_key, _platform), content in groups.items():
        for row in content:
            components = []
            for field in ("view_growth", "like_growth", "comment_growth", "share_growth"):
                value = row.get(field)
                population = [item[field] for item in content if item.get(field) is not None]
                if value is not None and population:
                    components.append(_percentile(float(value), population))
            row["momentum_score"] = sum(components) / len(components) if components else 0.0

        leader = max(content, key=lambda item: item["momentum_score"])
        elapsed_hours = max(0.0, float(leader.get("elapsed_hours") or 0))
        sample_count = int(leader.get("sample_count") or 0)
        group_size = len(content)
        if elapsed_hours >= 120 and sample_count >= 5 and group_size >= 5:
            confidence = "high"
        elif elapsed_hours >= 12 and sample_count >= 2 and group_size >= 3:
            confidence = "medium"
        else:
            confidence = "early"
        interactions = sum(
            leader.get(field) or 0
            for field in ("like_growth", "comment_growth", "share_growth")
        )
        growth_parts = []
        if leader.get("view_growth") is not None:
            growth_parts.append(f"{leader['view_growth']:,} new views")
        growth_parts.append(f"{interactions:,} new interactions")
        results.append(
            {
                **leader,
                "period_label": period_label,
                "confidence": confidence,
                "group_size": group_size,
                "message": (
                    f"“{leader.get('video_title') or leader['video_id']}” led {period_label} "
                    f"momentum with {' and '.join(growth_parts)} over "
                    f"{elapsed_hours:.0f} observed hours."
                ),
            }
        )

    return sorted(
        results,
        key=lambda item: (item.get("page_label", "").casefold(), item["platform"]),
    )[:limit]


def _percentile(value: float, population: list[int | float]) -> float:
    """Inclusive percentile rank; ties receive the same transparent score."""
    return 100.0 * sum(float(item) <= value for item in population) / len(population)


def audience_timing_focus(
    rows: list[dict],
    engagement_rows: list[dict] | None = None,
    quality_rows: list[dict] | None = None,
) -> list[dict]:
    """Find each channel/platform's strongest publishing window in IST.

    The score blends unique audience demand, comment intent quality, and
    observed view/like/share/comment growth so raw comment volume cannot
    dominate. Missing signals are dropped and the rest are reweighted.
    """
    comment_groups = _group_timing_rows(rows)
    engagement_groups = _group_timing_rows(engagement_rows or [])
    quality_groups = _group_timing_rows(quality_rows or [])
    keys = sorted(set(comment_groups) | set(engagement_groups) | set(quality_groups))

    results = []
    for page_key, platform in keys:
        comments = _timing_grid()
        demand = _timing_grid()
        quality = _timing_grid()
        engagement = _timing_grid()
        for row in comment_groups.get((page_key, platform), []):
            weekday, hour = _timing_slot(row)
            comment_count = float(row.get("comment_count") or 0)
            comments[weekday][hour] += comment_count
            demand[weekday][hour] += float(row.get("unique_authors") or comment_count)
        for row in quality_groups.get((page_key, platform), []):
            weekday, hour = _timing_slot(row)
            intent = classify_comment_intent(row.get("text") or "")
            quality[weekday][hour] += INTENT_WEIGHTS.get(intent, INTENT_WEIGHTS["other"])
        for row in engagement_groups.get((page_key, platform), []):
            weekday, hour = _timing_slot(row)
            engagement[weekday][hour] += _timing_engagement_score(row)
        score, used_signals = _blend_timing_grids(
            {
                "demand": demand,
                "quality": quality,
                "engagement": engagement,
            }
        )
        total_comments = _grid_total(comments)
        total_demand = _grid_total(demand)
        total_engagement = _grid_total(engagement)
        peak = max(
            (
                (day_start_hour, day_window_score, day)
                for day in range(7)
                for day_start_hour, day_window_score in [_best_same_day_window(score[day])]
            ),
            key=lambda item: (item[1], item[2], item[0]),
        )
        start_hour, window_score, best_day = peak
        if _grid_total(score) <= 0:
            window_score, best_day, start_hour = 0.0, 0, 0
            window_label = "Insufficient data"
        else:
            window_label = (
                f"{_hour_label(start_hour)}–{_hour_label(start_hour + 3)} IST"
            )
        peak_cell = max((score[day][hour], day, hour) for day in range(7) for hour in range(24))
        window_comments = sum(comments[best_day][start_hour + offset] for offset in range(3))
        confidence = _timing_confidence(
            total_comments=total_comments,
            total_demand=total_demand,
            total_engagement=total_engagement,
            signal_count=len(used_signals),
        )
        weekday_windows = [
            _weekday_publish_window(
                score[day],
                day,
                comments=comments[day],
                demand=demand[day],
                engagement=engagement[day],
            )
            for day in range(7)
        ]
        signal_label = ", ".join(used_signals) if used_signals else "comment volume"
        results.append(
            {
                "page_key": page_key,
                "platform": platform,
                "comment_count": int(total_comments),
                "unique_authors": int(round(total_demand)),
                "engagement_score": total_engagement,
                "window_count": int(window_comments),
                "window_score": window_score,
                "window_share": window_score / _grid_total(score) if _grid_total(score) else 0,
                "confidence": confidence,
                "signals": used_signals,
                "best_day": WEEKDAYS[best_day],
                "best_day_index": best_day,
                "window_start_hour": start_hour,
                "window": window_label,
                "heatmap": comments,
                "score_heatmap": score,
                "hour_labels": [_compact_hour(hour) for hour in range(24)],
                "weekday_labels": list(WEEKDAYS),
                "weekday_windows": weekday_windows,
                "peak_count": peak_cell[0],
                "message": (
                    f"Keep collecting comments and engagement snapshots before choosing a publish window."
                    if window_score <= 0
                    else (
                        f"The strongest audience window is {WEEKDAYS[best_day]} between "
                        f"{_hour_label(start_hour)} and {_hour_label(start_hour + 3)} IST, "
                        f"using {signal_label}. Test publishing or being available to "
                        "reply then, and use the weekday schedule for same-day times."
                    )
                ),
            }
        )
    return results


def _group_timing_rows(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("platform") is None:
            continue
        groups[(row.get("page_key") or "", row["platform"])].append(row)
    return groups


def _timing_slot(row: dict) -> tuple[int, int]:
    return int(row["weekday_ist"]) % 7, int(row["hour_ist"]) % 24


def _timing_grid() -> list[list[float]]:
    return [[0.0] * 24 for _ in range(7)]


def _grid_total(grid: list[list[float]]) -> float:
    return float(sum(sum(day) for day in grid))


def _timing_engagement_score(row: dict) -> float:
    likes = float(row.get("like_growth") or 0)
    comments = float(row.get("comment_growth") or 0)
    shares = float(row.get("share_growth") or 0)
    views = float(row.get("view_growth") or 0)
    return likes + (2 * comments) + (3 * shares) + (0.02 * views)


def _blend_timing_grids(grids: dict[str, list[list[float]]]) -> tuple[list[list[float]], list[str]]:
    """Normalize each available signal to its own peak, then weight them."""
    available = []
    for name, grid in grids.items():
        total = _grid_total(grid)
        if total <= 0:
            continue
        peak = max(value for day in grid for value in day) or 1.0
        available.append((name, TIMING_SIGNAL_WEIGHTS[name], peak, grid))
    blended = _timing_grid()
    if not available:
        return blended, []
    weight_total = sum(weight for _name, weight, _peak, _grid in available)
    labels = {
        "demand": "audience demand",
        "quality": "comment intent",
        "engagement": "engagement growth",
    }
    for name, weight, peak, grid in available:
        share = weight / weight_total
        for day in range(7):
            for hour in range(24):
                blended[day][hour] += share * (grid[day][hour] / peak)
    return blended, [labels[name] for name, _weight, _peak, _grid in available]


def _timing_confidence(
    *,
    total_comments: float,
    total_demand: float,
    total_engagement: float,
    signal_count: int,
) -> str:
    if signal_count >= 2 and total_demand >= 40 and total_engagement >= 80:
        return "high"
    if total_comments >= 100 and signal_count >= 2:
        return "high"
    if total_demand >= 12 or total_engagement >= 20 or total_comments >= 25:
        return "medium"
    return "early"


def _weekday_publish_window(
    score_hours: list[float],
    day: int,
    *,
    comments: list[float],
    demand: list[float],
    engagement: list[float],
) -> dict:
    """Best contiguous three-hour window that stays on one weekday."""
    day_score = sum(score_hours)
    start_hour, window_score = _best_same_day_window(score_hours)
    window_comments = sum(comments[start_hour + offset] for offset in range(3))
    window_demand = sum(demand[start_hour + offset] for offset in range(3))
    window_engagement = sum(engagement[start_hour + offset] for offset in range(3))
    has_signal = day_score > 0
    day_comments = sum(comments)
    if not has_signal:
        confidence = "none"
    elif day_comments >= 25 or window_engagement >= 40:
        confidence = "high"
    elif day_comments >= 8 or window_engagement >= 10 or window_demand >= 5:
        confidence = "medium"
    else:
        confidence = "early"
    return {
        "day": WEEKDAYS[day],
        "day_index": day,
        "day_count": int(round(day_comments)),
        "day_score": day_score,
        "unique_authors": int(round(sum(demand))),
        "engagement_score": window_engagement,
        "window_count": int(round(window_comments)),
        "window_score": window_score,
        "audience_score": int(round((window_score / 3.0) * 100)) if has_signal else 0,
        "window_share": window_score / day_score if day_score else 0,
        "window_start_hour": start_hour if has_signal else None,
        "window": (
            f"{_hour_label(start_hour)}–{_hour_label(start_hour + 3)} IST"
            if has_signal
            else "Insufficient data"
        ),
        "confidence": confidence,
        "has_signal": has_signal,
    }


def _best_same_day_window(day_hours: list[float] | list[int]) -> tuple[int, float]:
    """Return (start_hour, score) for the strongest 3-hour block in 00:00–24:00.

    Tied windows prefer the block whose middle hour is closest to the day's
    peak hour, so the recommendation sits on the audience spike rather than
    starting two hours early.
    """
    peak_hour = max(range(24), key=lambda hour: (day_hours[hour], -hour))
    peak = max(
        (
            sum(day_hours[start + offset] for offset in range(3)),
            -abs(start + 1 - peak_hour),
            -start,
        )
        for start in range(22)
    )
    window_score, _distance, neg_start = peak
    return -neg_start, window_score


def _compact_hour(hour: int) -> str:
    suffix = "a" if hour < 12 else "p"
    return f"{hour % 12 or 12}{suffix}"


def comment_intent_focus(rows: list[dict]) -> list[dict]:
    """Summarize audience intent locally with deterministic, auditable rules."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row.get("page_key") or "", row["platform"])].append(row)
    guidance = {
        "question": "Create a Q&A or explanatory post addressing recurring audience questions.",
        "request": "Treat repeated requests as candidates for the next content topic.",
        "complaint": "Review the concern and address it clearly before promoting related content.",
        "praise": "Repeat the themes and format that are generating positive audience response.",
        "other": "Keep collecting comments before choosing an intent-led content direction.",
    }
    results = []
    for (page_key, platform), comments in groups.items():
        counts = {name: 0 for name in guidance}
        for row in comments:
            counts[classify_comment_intent(row.get("text") or "")] += 1
        meaningful = {key: value for key, value in counts.items() if key != "other"}
        leading = max(meaningful, key=meaningful.get) if any(meaningful.values()) else "other"
        total = len(comments)
        results.append(
            {
                "page_key": page_key,
                "platform": platform,
                "sample_size": total,
                "leading_intent": leading,
                "leading_count": counts[leading],
                "share": counts[leading] / total if total else 0,
                "confidence": "high" if total >= 100 else "medium" if total >= 25 else "early",
                "message": guidance[leading],
            }
        )
    return sorted(results, key=lambda item: (item["page_key"], item["platform"]))


def classify_comment_intent(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    words = set(re.findall(r"[\w']+", normalized, flags=re.UNICODE))
    if words & {"problem", "issue", "wrong", "bad", "broken", "complaint", "disappointed"}:
        return "complaint"
    if "?" in normalized or words & {"what", "when", "where", "why", "how", "which", "kya", "kab", "kahan", "kaise"}:
        return "question"
    if words & {"please", "request", "suggest", "cover", "visit", "show", "make"}:
        return "request"
    if words & {"great", "beautiful", "amazing", "love", "nice", "excellent", "wonderful", "thanks", "thank"}:
        return "praise"
    return "other"


def _hour_label(hour: int) -> str:
    hour = hour % 24
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:00 {suffix}"


def recommendation_key(kind: str, item: dict) -> str:
    identity = "|".join(
        (
            kind,
            item.get("platform") or "",
            item.get("page_key") or "",
            item.get("video_id") or "",
            item.get("period_label") or "",
            item.get("message") or "",
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
