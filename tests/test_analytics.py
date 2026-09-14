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


if __name__ == "__main__":
    unittest.main()
