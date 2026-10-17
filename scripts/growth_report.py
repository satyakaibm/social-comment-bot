#!/usr/bin/env python3
"""Write the Momentum growth charts and direction to a folder, offline.

Same charts and the same written direction as the "Growth charts and
direction" section of the Momentum page (app/growth_charts.py), as PNG files
plus growth_direction.md and a CSV behind each chart. Useful for sharing a
monthly snapshot or for working on the charts without the dashboard.

    .venv/bin/python scripts/growth_report.py
    .venv/bin/python scripts/growth_report.py --platform youtube --page-key hindolroad
    .venv/bin/python scripts/growth_report.py --db data/comments.db --out reports/growth

Reads only. Point it at a fresh copy of the live database
(scripts/sync_db_from_vm.sh), never at the VM itself.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import config, db, growth_charts  # noqa: E402

TABLE_KEYS = ("scoreboard", "daily", "comments_per_day", "movers", "rates", "intents",
              "coverage", "view_peaks", "comment_peaks", "online_profile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=None, help="path to comments.db (default: the app's data/comments.db)")
    parser.add_argument("--platform", default="", choices=("", *growth_charts.PLATFORMS))
    parser.add_argument("--page-key", default="", help="limit to one configured page/channel")
    parser.add_argument("--days", type=int, default=config.YOUTUBE_STATS_MAX_RETENTION_DAYS,
                        help="window in days (capped at 30: stats history is pruned there)")
    parser.add_argument("--out", default=None, help="output folder (default: reports/growth/<date>)")
    args = parser.parse_args(argv)
    if not growth_charts.available():
        print("pandas, matplotlib and seaborn are required: pip install -r requirements.txt", file=sys.stderr)
        return 2
    days = max(1, min(args.days, config.YOUTUBE_STATS_MAX_RETENTION_DAYS))
    out = Path(args.out) if args.out else BASE_DIR / "reports" / "growth" / datetime.now(growth_charts.IST).strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)

    conn = db.open_connection(args.db, migrate_plaintext=False)
    try:
        dataset = growth_charts.build_dataset(
            conn, platform=args.platform or None, page_key=args.page_key or None, days=days,
        )
    finally:
        conn.close()

    written: list[str] = []
    for name in growth_charts.CHARTS:
        png = growth_charts.render(dataset, name)
        if png:
            (out / f"{name}.png").write_bytes(png)
            written.append(name)
    for key in TABLE_KEYS:
        frame = dataset.get(key)
        if frame is not None and not frame.empty:
            frame.to_csv(out / f"{key}.csv", index=False)
    result = growth_charts.direction(dataset)
    (out / "growth_direction.md").write_text(
        growth_charts.direction_markdown(dataset, result, written), encoding="utf-8",
    )
    print(f"Report written to {out}")
    print("Read growth_direction.md first; the charts and CSV tables are alongside it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
