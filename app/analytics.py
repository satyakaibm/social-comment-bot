from collections import defaultdict
from datetime import datetime, timedelta, timezone
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
    *,
    now: "datetime | None" = None,
) -> list[dict]:
    """Find when each channel/platform's existing audience engages, in IST.

    This is a reply-timing signal, not a publish-timing one: it is built
    from when people commented and when snapshots saw likes/shares/views
    move, i.e. from the slice of the audience that already interacts.
    Follower presence -- what the platforms' own "most active times" show
    -- is a different population; see instagram_online_focus for the one
    platform that exposes it.

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
        window_demand = sum(demand[best_day][start_hour + offset] for offset in range(3))
        window_engagement = sum(engagement[best_day][start_hour + offset] for offset in range(3))
        confidence = _timing_confidence(
            total_comments=total_comments,
            total_demand=total_demand,
            total_engagement=total_engagement,
            signal_count=len(used_signals),
            window_demand=window_demand,
            window_engagement=window_engagement,
        )
        # Pooled across weekdays: 7x the evidence per hour, for when any
        # single weekday is too thin to say anything on its own.
        pooled_score = [sum(score[day][hour] for day in range(7)) for hour in range(24)]
        pooled_start, pooled_total = _best_same_day_window(pooled_score)
        pooled_demand = sum(
            demand[day][pooled_start + offset] for day in range(7) for offset in range(3)
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
        next_window = next_engagement_window(weekday_windows, now=now)
        results.append(
            {
                "page_key": page_key,
                "platform": platform,
                "next_window": next_window,
                "pooled_window": (
                    f"{_hour_label(pooled_start)}–{_hour_label(pooled_start + 3)} IST"
                    if pooled_total > 0 else "Insufficient data"
                ),
                "pooled_window_demand": int(round(pooled_demand)),
                "window_demand": int(round(window_demand)),
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
                    "Keep collecting comments and engagement snapshots before reading "
                    "anything into this."
                    if window_score <= 0
                    else (
                        f"The people who already engage with you are most active on "
                        f"{WEEKDAYS[best_day]}s between {_hour_label(start_hour)} and "
                        f"{_hour_label(start_hour + 3)} IST, from {signal_label}. That is "
                        "the best time to be around to reply. It is not a measure of "
                        "when to publish: for that use the Instagram card above, or the "
                        "platform's own dashboard."
                    )
                ),
            }
        )
    return results


IST = timezone(timedelta(hours=5, minutes=30))
# Hours of a weekday that must be observed before its window is ranked.
WEEKDAY_MIN_HOURS = 20


def instagram_online_focus(rows: list[dict], *, limit: int = 4) -> list[dict]:
    """Present Instagram's own follower-online counts the way its app does.

    `rows` are audience_online rows (one per day bucket and UTC hour). Nothing
    here is modelled: the hour profile is the per-hour average of Instagram's
    counts over the days we hold, and the window is the best three
    consecutive hours of that average. The only transformation is the clock:
    Meta reports UTC hours, so each slot is shown as the IST hour it maps to
    (UTC 06:00 -> 11:30 AM IST). Slots are ordered by IST so the chart reads
    midnight to midnight locally.

    Weekday rows attribute each UTC hour to the calendar day it actually
    fell in: a bucket ending at 07:00Z covers the previous day's 07-23Z plus
    this day's 00-06Z.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row.get("page_key") or ""].append(row)

    results = []
    for page_key, page_rows in groups.items():
        by_day: dict[str, dict[int, int]] = defaultdict(dict)
        for row in page_rows:
            by_day[row["bucket_end"]][int(row["hour_utc"])] = int(row["online_count"])
        if not by_day:
            continue
        days = len(by_day)
        hour_total = [0.0] * 24
        hour_samples = [0] * 24
        weekday_total = [[0.0] * 24 for _ in range(7)]
        weekday_days: list[set] = [set() for _ in range(7)]
        weekday_hours: list[set] = [set() for _ in range(7)]
        for bucket_end, hours in by_day.items():
            end = _parse_bucket_end(bucket_end)
            for hour_utc, count in hours.items():
                hour_total[hour_utc] += count
                hour_samples[hour_utc] += 1
                if end is None:
                    continue
                # Hours at or after the bucket's own clock hour belong to
                # the previous calendar day (UTC); earlier ones to this day.
                day = end.date() if hour_utc < end.hour else (end - timedelta(days=1)).date()
                moment = datetime(day.year, day.month, day.day, hour_utc, tzinfo=timezone.utc)
                local = moment.astimezone(IST)
                weekday = (local.weekday() + 1) % 7  # WEEKDAYS starts on Sunday
                weekday_total[weekday][hour_utc] += count
                # Count sample days on the IST calendar, the same one the
                # weekday came from. An IST day straddles two UTC dates, so
                # counting UTC dates here reported "2 Wednesdays" for one
                # Wednesday and halved every weekday's average.
                weekday_days[weekday].add(local.date())
                weekday_hours[weekday].add(hour_utc)
        average = [
            hour_total[h] / hour_samples[h] if hour_samples[h] else 0.0 for h in range(24)
        ]
        peak_value = max(average) or 1.0
        start_utc, window_avg = _best_circular_window(average)
        window_hours = {(start_utc + offset) % 24 for offset in range(3)}
        peak_utc = max(range(24), key=lambda h: (average[h], -h))
        profile = sorted(
            (
                {
                    "hour_utc": h,
                    "label": _ist_slot_label(h),
                    "avg": int(round(average[h])),
                    "share": int(round(100 * average[h] / peak_value)),
                    "in_window": h in window_hours,
                    "is_peak": h == peak_utc,
                    "ist_minutes": _ist_minutes(h),
                }
                for h in range(24)
            ),
            key=lambda slot: slot["ist_minutes"],
        )
        weekday_windows = []
        for day in range(7):
            samples = len(weekday_days[day])
            # A bucket ends at 07:00Z, so the newest one holds only the first
            # seven UTC hours of its final calendar day. Ranking a window on
            # a weekday seen for seven hours "finds" the best of those seven
            # (live data showed Sunday at 5:30 AM for exactly this reason),
            # so a weekday counts only once nearly its whole day is covered.
            if samples and len(weekday_hours[day]) >= WEEKDAY_MIN_HOURS:
                day_avg = [weekday_total[day][h] / samples for h in range(24)]
                day_start, day_score = _best_circular_window(day_avg)
                weekday_windows.append(
                    {
                        "day": WEEKDAYS[day],
                        "day_index": day,
                        "window": _ist_window_label(day_start),
                        "avg_online": int(round(day_score / 3)),
                        "samples": samples,
                        "has_signal": True,
                    }
                )
            else:
                weekday_windows.append(
                    {"day": WEEKDAYS[day], "day_index": day, "window": "—",
                     "avg_online": 0, "samples": 0, "has_signal": False,
                     "partial": bool(samples)}
                )
        best_weekday = max(
            (w for w in weekday_windows if w["has_signal"]),
            key=lambda w: w["avg_online"],
            default=None,
        )
        confidence = "high" if days >= 14 else "medium" if days >= 7 else "early"
        latest = max(by_day)
        latest_end = _parse_bucket_end(latest)
        results.append(
            {
                "page_key": page_key,
                "platform": "instagram",
                "days": days,
                "latest_day": (
                    (latest_end - timedelta(days=1)).astimezone(IST).strftime("%a %d %b")
                    if latest_end else latest[:10]
                ),
                "window": _ist_window_label(start_utc),
                "window_avg_online": int(round(window_avg / 3)),
                "peak_label": _ist_slot_label(peak_utc),
                "peak_online": int(round(average[peak_utc])),
                "hour_profile": profile,
                "weekday_windows": weekday_windows,
                "best_weekday": best_weekday["day"] if best_weekday else "",
                "best_weekday_index": best_weekday["day_index"] if best_weekday else None,
                "confidence": confidence,
                "message": (
                    f"Instagram counted an average of {int(round(window_avg / 3)):,} followers "
                    f"online per hour between {_ist_window_label(start_utc)} across the last "
                    f"{days} day(s) -- the same figures behind the app's Most active times. "
                    "Publish just before the window so the post is live when they arrive."
                ),
            }
        )
    return sorted(results, key=lambda item: item["page_key"])[:limit]


def _parse_bucket_end(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None


def _best_circular_window(hours: list[float]) -> tuple[int, float]:
    """(start_hour, summed score) of the best 3-hour block, wrapping midnight.

    Follower presence is a daily cycle with no day boundary of its own, so a
    window may cross midnight; ties go to the earlier start.
    """
    best = max(
        (sum(hours[(start + offset) % 24] for offset in range(3)), -start)
        for start in range(24)
    )
    return -best[1], best[0]


def _ist_minutes(hour_utc: int) -> int:
    return (hour_utc * 60 + 330) % 1440


def _ist_slot_label(hour_utc: int) -> str:
    minutes = _ist_minutes(hour_utc)
    hour, minute = divmod(minutes, 60)
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def _ist_window_label(start_utc: int) -> str:
    return f"{_ist_slot_label(start_utc)}–{_ist_slot_label((start_utc + 3) % 24)} IST"


def next_engagement_window(weekday_windows: list[dict], *, now=None) -> dict | None:
    """The soonest upcoming weekday window with signal, from `now` (IST).

    Searches today first (a window still ahead today, or in progress, wins),
    then the following days, and finally today's weekday again a week out --
    so a single-weekday signal always resolves to a real upcoming time rather
    than a bare weekday name that reads like a date.
    """
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    today = (now.weekday() + 1) % 7
    for offset in range(8):
        entry = weekday_windows[(today + offset) % 7]
        if not entry.get("has_signal") or entry.get("window_start_hour") is None:
            continue
        day = now.date() + timedelta(days=offset)
        start = datetime(day.year, day.month, day.day, int(entry["window_start_hour"]), tzinfo=IST)
        end = start + timedelta(hours=3)
        if end <= now:
            continue
        in_progress = start <= now
        hours_until = max(0.0, (start - now).total_seconds() / 3600)
        if offset == 0:
            when = "today"
        elif offset == 1:
            when = "tomorrow"
        else:
            when = start.strftime("%A %d %b")
        if in_progress:
            starts_in = "in progress now"
        elif hours_until < 1:
            starts_in = f"in {int(round(hours_until * 60))} min"
        elif hours_until < 24:
            starts_in = f"in {int(round(hours_until))} h"
        else:
            starts_in = f"in {int(hours_until // 24)} day(s)"
        return {
            "when_label": when,
            "window": entry["window"],
            "day": entry["day"],
            "day_index": entry["day_index"],
            "in_progress": in_progress,
            "starts_in": starts_in,
            "confidence": entry.get("confidence", ""),
        }
    return None


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
    """Growth that viewers caused, for timing purposes.

    comment_growth is deliberately left out. It comes from the platform's
    comment_count, which includes the bot's own replies -- posted seconds
    after each incoming comment via webhooks -- so it mostly re-measured
    comment arrival plus the bot's reply timing. Comment arrival is already
    the demand grid; counting it twice, once inflated, pulled every window
    toward whenever the bot was busiest.
    """
    likes = float(row.get("like_growth") or 0)
    shares = float(row.get("share_growth") or 0)
    views = float(row.get("view_growth") or 0)
    return likes + (3 * shares) + (0.02 * views)


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


# A window needs this many distinct commenters -- or this much viewer growth
# -- inside its own three hours before it is more than a guess, whatever the
# totals across the grid say.
WINDOW_MIN_DEMAND = 5
WINDOW_MIN_ENGAGEMENT = 10


def _timing_confidence(
    *,
    total_comments: float,
    total_demand: float,
    total_engagement: float,
    signal_count: int,
    window_demand: float = 0.0,
    window_engagement: float = 0.0,
) -> str:
    # Grid totals used to be the only input, so 100 comments spread thinly
    # over 168 cells could rate "high" for a window that itself held two
    # of them. The window's own evidence gates everything below.
    if window_demand < WINDOW_MIN_DEMAND and window_engagement < WINDOW_MIN_ENGAGEMENT:
        return "early"
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
