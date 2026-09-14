from collections import defaultdict


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
