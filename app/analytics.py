from collections import defaultdict
import re


WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


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


def audience_timing_focus(rows: list[dict]) -> list[dict]:
    """Find each channel/platform's busiest day and rolling three-hour IST window."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row.get("page_key") or "", row["platform"])].append(row)

    results = []
    for (page_key, platform), activity in groups.items():
        hourly = [0] * 24
        daily = [0] * 7
        for row in activity:
            count = int(row["comment_count"])
            hourly[int(row["hour_ist"])] += count
            daily[int(row["weekday_ist"])] += count
        total = sum(hourly)
        start_hour = max(range(24), key=lambda hour: sum(hourly[(hour + offset) % 24] for offset in range(3)))
        end_hour = (start_hour + 3) % 24
        best_day = max(range(7), key=lambda day: daily[day])
        confidence = "high" if total >= 100 else "medium" if total >= 25 else "early"
        results.append(
            {
                "page_key": page_key,
                "platform": platform,
                "comment_count": total,
                "confidence": confidence,
                "best_day": WEEKDAYS[best_day],
                "window": f"{_hour_label(start_hour)}–{_hour_label(end_hour)} IST",
                "message": (
                    f"Audience comments peak on {WEEKDAYS[best_day]}; test publishing or "
                    f"being available to reply around {_hour_label(start_hour)}–{_hour_label(end_hour)} IST."
                ),
            }
        )
    return sorted(results, key=lambda item: (item["page_key"], item["platform"]))


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
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:00 {suffix}"
