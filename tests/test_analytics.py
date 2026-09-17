import unittest

from app import analytics


class CreatorFocusTests(unittest.TestCase):
    def test_recommendations_are_grouped_by_channel_and_platform(self):
        rows = [
            {
                "platform": "youtube", "page_key": "travel",
                "page_label": "Travel Explorer", "video_id": "a",
                "video_title": "Beach", "view_count": 1000,
                "like_count": 20, "comment_count": 10, "share_count": None,
            },
            {
                "platform": "youtube", "page_key": "travel",
                "page_label": "Travel Explorer", "video_id": "b",
                "video_title": "Mountains", "view_count": 1000,
                "like_count": 10, "comment_count": 2, "share_count": None,
            },
            {
                "platform": "instagram", "page_key": "hindolroad",
                "page_label": "Hindolroad", "video_id": "c",
                "video_title": "Aarti", "view_count": None,
                "like_count": 12, "comment_count": 4, "share_count": None,
            },
        ]

        recommendations = analytics.creator_focus(rows)

        self.assertEqual(len(recommendations), 2)
        travel = next(item for item in recommendations if item["page_key"] == "travel")
        self.assertEqual(travel["title"], "Beach")
        self.assertEqual(travel["confidence"], "early")
        self.assertIn("Beach", travel["message"])

    def test_view_based_score_is_an_engagement_rate(self):
        high_rate = {
            "view_count": 100, "like_count": 10,
            "comment_count": 0, "share_count": 0,
        }
        large_but_low_rate = {
            "view_count": 10000, "like_count": 100,
            "comment_count": 0, "share_count": 0,
        }

        self.assertGreater(
            analytics._engagement_score(high_rate),
            analytics._engagement_score(large_but_low_rate),
        )

    def test_momentum_is_ranked_within_channel_platform(self):
        rows = [
            {
                "platform": "youtube", "page_key": "travel",
                "page_label": "Travel Explorer", "video_id": "fast",
                "video_title": "Fast", "view_growth": 100,
                "like_growth": 20, "comment_growth": 8, "share_growth": None,
                "elapsed_hours": 20, "sample_count": 3,
            },
            {
                "platform": "youtube", "page_key": "travel",
                "page_label": "Travel Explorer", "video_id": "slow",
                "video_title": "Slow", "view_growth": 10,
                "like_growth": 2, "comment_growth": 1, "share_growth": None,
                "elapsed_hours": 20, "sample_count": 3,
            },
            {
                "platform": "youtube", "page_key": "travel",
                "page_label": "Travel Explorer", "video_id": "middle",
                "video_title": "Middle", "view_growth": 50,
                "like_growth": 5, "comment_growth": 3, "share_growth": None,
                "elapsed_hours": 20, "sample_count": 3,
            },
        ]

        recommendations = analytics.momentum_focus(rows, period_label="24-hour")

        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0]["video_id"], "fast")
        self.assertEqual(recommendations[0]["momentum_score"], 100)
        self.assertEqual(recommendations[0]["confidence"], "medium")
        self.assertIn("100 new views", recommendations[0]["message"])

    def test_audience_timing_finds_rolling_three_hour_window(self):
        rows = [
            {"platform": "youtube", "page_key": "travel", "weekday_ist": 5, "hour_ist": 18, "comment_count": 8},
            {"platform": "youtube", "page_key": "travel", "weekday_ist": 5, "hour_ist": 19, "comment_count": 10},
            {"platform": "youtube", "page_key": "travel", "weekday_ist": 6, "hour_ist": 20, "comment_count": 7},
            {"platform": "youtube", "page_key": "travel", "weekday_ist": 1, "hour_ist": 9, "comment_count": 2},
        ]

        result = analytics.audience_timing_focus(rows)[0]

        self.assertEqual(result["best_day"], "Friday")
        self.assertEqual(result["window"], "6:00 PM–9:00 PM IST")
        self.assertEqual(result["confidence"], "medium")
        friday = result["weekday_windows"][5]
        monday = result["weekday_windows"][1]
        sunday = result["weekday_windows"][0]
        self.assertEqual(friday["window"], "6:00 PM–9:00 PM IST")
        self.assertEqual(friday["window_count"], 18)
        self.assertEqual(monday["window"], "8:00 AM–11:00 AM IST")
        self.assertEqual(sunday["window"], "Insufficient data")
        self.assertFalse(sunday["has_signal"])

    def test_audience_timing_keeps_platforms_and_weekdays_separate(self):
        rows = [
            {"platform": "youtube", "page_key": "travel", "weekday_ist": 1, "hour_ist": 9, "comment_count": 6},
            {"platform": "youtube", "page_key": "travel", "weekday_ist": 1, "hour_ist": 10, "comment_count": 8},
            {"platform": "instagram", "page_key": "travel", "weekday_ist": 6, "hour_ist": 20, "comment_count": 9},
            {"platform": "instagram", "page_key": "travel", "weekday_ist": 6, "hour_ist": 21, "comment_count": 11},
        ]

        results = {item["platform"]: item for item in analytics.audience_timing_focus(rows)}

        self.assertEqual(results["youtube"]["weekday_windows"][1]["window"], "9:00 AM–12:00 PM IST")
        self.assertEqual(results["instagram"]["weekday_windows"][6]["window"], "8:00 PM–11:00 PM IST")
        self.assertEqual(results["youtube"]["best_day"], "Monday")
        self.assertEqual(results["instagram"]["best_day"], "Saturday")

    def test_headline_window_does_not_merge_unrelated_late_and_early_hours(self):
        # A burst at 11pm Saturday and an unrelated burst at 1am Saturday are
        # ~22 hours apart, not adjacent -- the headline window must not
        # wrap hour 23 back to hour 0 of the same day and merge them into a
        # fabricated "11 PM-2 AM" window.
        rows = [
            {"platform": "youtube", "page_key": "p", "weekday_ist": 6, "hour_ist": 23, "comment_count": 20},
            {"platform": "youtube", "page_key": "p", "weekday_ist": 6, "hour_ist": 1, "comment_count": 20},
        ]

        result = analytics.audience_timing_focus(rows)[0]

        self.assertIn(result["window_start_hour"], (0, 21, 22, 23))
        self.assertLessEqual(result["window_start_hour"] + 3, 24)
        self.assertEqual(result["window_count"], 20)

    def test_same_day_window_covers_late_evening_without_wrapping(self):
        hours = [0] * 24
        hours[22] = 4
        hours[23] = 5

        start_hour, window_count = analytics._best_same_day_window(hours)

        self.assertEqual(start_hour, 21)
        self.assertEqual(window_count, 9)

    def test_unique_commenters_outrank_repeat_comment_spam(self):
        rows = [
            {
                "platform": "youtube", "page_key": "travel", "weekday_ist": 1,
                "hour_ist": 9, "comment_count": 20, "unique_authors": 2,
            },
            {
                "platform": "youtube", "page_key": "travel", "weekday_ist": 5,
                "hour_ist": 18, "comment_count": 8, "unique_authors": 8,
            },
        ]

        result = analytics.audience_timing_focus(rows)[0]

        self.assertEqual(result["best_day"], "Friday")
        self.assertEqual(result["weekday_windows"][5]["window"], "5:00 PM–8:00 PM IST")

    def test_intent_and_engagement_can_shift_the_publish_window(self):
        comments = [
            {
                "platform": "instagram", "page_key": "travel", "weekday_ist": 1,
                "hour_ist": 9, "comment_count": 6, "unique_authors": 6,
            },
        ]
        quality = [
            {
                "platform": "instagram", "page_key": "travel", "weekday_ist": 6,
                "hour_ist": 20, "text": "When is the next aarti?",
            },
            {
                "platform": "instagram", "page_key": "travel", "weekday_ist": 6,
                "hour_ist": 20, "text": "How can I visit this temple?",
            },
            {
                "platform": "instagram", "page_key": "travel", "weekday_ist": 6,
                "hour_ist": 21, "text": "Please cover Puri next",
            },
        ]
        engagement = [
            {
                "platform": "instagram", "page_key": "travel", "weekday_ist": 6,
                "hour_ist": 20, "view_growth": 400, "like_growth": 40,
                "comment_growth": 12, "share_growth": 6,
            },
        ]

        result = analytics.audience_timing_focus(
            comments, engagement_rows=engagement, quality_rows=quality,
        )[0]

        self.assertEqual(result["best_day"], "Saturday")
        self.assertEqual(result["weekday_windows"][6]["window"], "7:00 PM–10:00 PM IST")
        self.assertIn("engagement growth", result["signals"])
        self.assertIn("comment intent", result["signals"])

    def test_comment_intent_rules_are_local_and_auditable(self):
        self.assertEqual(analytics.classify_comment_intent("When is the next trip?"), "question")
        self.assertEqual(analytics.classify_comment_intent("Please visit Odisha"), "request")
        self.assertEqual(analytics.classify_comment_intent("The link is broken"), "complaint")
        self.assertEqual(analytics.classify_comment_intent("Beautiful video, thanks"), "praise")

    def test_comment_intent_focus_recommends_from_leading_intent(self):
        rows = [
            {"platform": "instagram", "page_key": "travel", "text": "Where is this?"},
            {"platform": "instagram", "page_key": "travel", "text": "How can I go?"},
            {"platform": "instagram", "page_key": "travel", "text": "Beautiful"},
        ]

        result = analytics.comment_intent_focus(rows)[0]

        self.assertEqual(result["leading_intent"], "question")
        self.assertEqual(result["leading_count"], 2)
        self.assertIn("Q&amp;A", result["message"].replace("&", "&amp;"))


if __name__ == "__main__":
    unittest.main()
