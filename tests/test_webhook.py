import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, db, webhook


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def test_verification_requires_matching_token(self):
        app = webhook.create_app(start_worker=False)
        with patch.object(config, "META_WEBHOOK_VERIFY_TOKEN", "verify"):
            client = app.test_client()
            response = client.get(
                "/webhooks/meta?hub.mode=subscribe&hub.verify_token=verify&hub.challenge=123"
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "123")
            self.assertEqual(
                client.get(
                    "/webhooks/meta?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=123"
                ).status_code,
                403,
            )

    def test_root_shows_service_status(self):
        response = webhook.create_app(start_worker=False).test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    def test_signed_payload_is_queued_once(self):
        payload = {
            "object": "instagram",
            "entry": [{"changes": [{"field": "comments", "value": {
                "id": "comment", "text": "Jai Maa",
                "from": {"username": "viewer"}, "media": {"id": "media"},
            }}]}],
        }
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        app = webhook.create_app(start_worker=False)
        with patch.object(config, "META_APP_SECRET", "secret"):
            client = app.test_client()
            first = client.post(
                "/webhooks/meta", data=body, content_type="application/json",
                headers={"X-Hub-Signature-256": signature},
            )
            second = client.post(
                "/webhooks/meta", data=body, content_type="application/json",
                headers={"X-Hub-Signature-256": signature},
            )
        self.assertEqual(first.json["queued"], 1)
        self.assertEqual(second.json["queued"], 0)

    def test_old_comment_payload_is_not_queued(self):
        payload = {
            "object": "instagram",
            "entry": [{"changes": [{"field": "comments", "value": {
                "id": "old-comment", "text": "Jai Maa",
                "timestamp": "2020-01-01T00:00:00Z",
                "from": {"username": "viewer"}, "media": {"id": "media"},
            }}]}],
        }
        self.assertEqual(webhook.queue_payload(payload), 0)
        with db.connect() as conn:
            self.assertIsNone(db.get_comment(conn, "old-comment"))

    def test_unsigned_payload_is_rejected(self):
        app = webhook.create_app(start_worker=False)
        with patch.object(config, "META_APP_SECRET", "secret"):
            response = app.test_client().post(
                "/webhooks/meta", json={"object": "instagram", "entry": []}
            )
        self.assertEqual(response.status_code, 401)

    def test_facebook_reply_events_are_ignored(self):
        payload = {"object": "page", "entry": [{"changes": [{
            "field": "feed", "value": {
                "item": "comment", "verb": "add", "comment_id": "reply",
                "post_id": "post", "parent_id": "parent", "message": "reply",
            }
        }]}]}
        self.assertEqual(webhook.extract_comment_events(payload), [])

    def _facebook_comment_payload(self, entry_id: str, comment_id: str = "c1") -> dict:
        return {
            "object": "page",
            "entry": [{"id": entry_id, "changes": [{"field": "feed", "value": {
                "item": "comment", "verb": "add", "comment_id": comment_id,
                "post_id": "post", "message": "hi",
                "from": {"id": "viewer", "name": "Viewer"},
            }}]}],
        }

    def _with_second_page(self):
        second_page = config.PageConfig(
            key="second", label="Second", facebook_page_id="page-2",
            facebook_page_access_token="", meta_user_access_token="",
            instagram_user_id="", facebook_post_ids=[], instagram_media_ids=[],
            facebook_daily_reply_limit=0, instagram_daily_reply_limit=0,
            persona="", persona_dir=config.REPLY_EXAMPLES_DIR,
        )
        return (
            patch.object(config, "PAGES", {**config.PAGES, "second": second_page}),
            patch.object(config, "PAGES_BY_FACEBOOK_ID", {"page-2": "second"}),
        )

    def test_unmatched_entry_id_is_dropped_once_a_second_page_exists(self):
        pages_patch, ids_patch = self._with_second_page()
        with pages_patch, ids_patch:
            events = webhook.extract_comment_events(
                self._facebook_comment_payload("unknown-page")
            )
        self.assertEqual(events, [])

    def test_matched_entry_id_resolves_to_the_correct_page(self):
        pages_patch, ids_patch = self._with_second_page()
        with pages_patch, ids_patch:
            events = webhook.extract_comment_events(
                self._facebook_comment_payload("page-2")
            )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["page_key"], "second")

    def test_unmatched_entry_id_falls_back_to_default_with_only_one_page(self):
        # Preserves existing single-page webhook behavior/fixtures: none of
        # them include entry.id, and this must keep working exactly as
        # before as long as only one page is configured.
        events = webhook.extract_comment_events(
            self._facebook_comment_payload("")
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["page_key"], config.DEFAULT_PAGE_KEY)

    def test_event_is_drafted_posted_liked_and_recorded(self):
        event = {
            "platform": "instagram", "comment_id": "comment",
            "container_id": "media", "text": "Jai Maa",
            "author": "viewer", "author_id": "viewer-id", "published_at": "now",
        }
        with patch.object(config, "META_WEBHOOK_AUTO_POST", True), \
             patch.object(webhook.meta_client, "get_instagram_username", return_value="owner"), \
             patch.object(webhook.meta_client, "find_own_reply", return_value=None), \
             patch.object(webhook.meta_client, "get_instagram_media_caption", return_value="Caption"), \
             patch.object(webhook, "draft_reply", return_value="@viewer 🙏"), \
             patch.object(webhook.meta_client, "reply_to_comment", return_value="reply") as reply, \
             patch.object(webhook.meta_client, "like_comment") as like:
            webhook.process_event(event)
        reply.assert_called_once_with("comment", "@viewer 🙏", platform="instagram")
        like.assert_called_once_with("comment", platform="instagram")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT status, reply_comment_id FROM comments WHERE comment_id='comment'"
            ).fetchone()
        self.assertEqual((row["status"], row["reply_comment_id"]), ("posted", "reply"))

    def test_pruned_comment_id_is_not_queued_again(self):
        payload = {
            "object": "instagram",
            "entry": [{"changes": [{"field": "comments", "value": {
                "id": "old-comment", "text": "Jai Maa",
                "from": {"username": "viewer"}, "media": {"id": "media"},
            }}]}],
        }
        with db.connect() as conn:
            db.remember_seen_comment(
                conn, "old-comment", platform="instagram", status="posted"
            )
        queued = webhook.queue_payload(payload)
        self.assertEqual(queued, 0)
        with db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM webhook_events WHERE event_key = 'instagram:old-comment'"
            ).fetchone()
        self.assertIsNone(row)

    def test_daily_gemini_cap_saves_comment_without_drafting(self):
        # A webhook event can't be redelivered, so unlike the cron pollers
        # (which just leave the comment unseen for later), this must save
        # the comment rather than lose it, with an empty draft for a
        # manual reply from the dashboard.
        event = {
            "platform": "instagram", "comment_id": "comment",
            "container_id": "media", "text": "Jai Maa",
            "author": "viewer", "author_id": "viewer-id", "published_at": "now",
        }
        with patch.object(config, "GEMINI_DAILY_DRAFT_LIMIT", 1), \
             patch.object(webhook.db, "count_drafted_today", return_value=1), \
             patch.object(webhook.meta_client, "get_instagram_username", return_value="owner"), \
             patch.object(webhook.meta_client, "find_own_reply", return_value=None), \
             patch.object(webhook.meta_client, "get_instagram_media_caption", return_value="Caption"), \
             patch.object(webhook, "draft_reply") as draft, \
             patch.object(webhook.meta_client, "reply_to_comment") as reply:
            webhook.process_event(event)
        draft.assert_not_called()
        reply.assert_not_called()
        with db.connect() as conn:
            row = db.get_comment(conn, "comment")
        self.assertEqual(row["status"], "pending_review")
        self.assertEqual(row["draft_reply"], "")
        self.assertIn("Gemini daily draft limit", row["error"])

    def test_process_event_skips_seen_comment_without_drafting(self):
        event = {
            "platform": "instagram", "comment_id": "old-comment",
            "container_id": "media", "text": "Jai Maa",
            "author": "viewer", "author_id": "viewer-id", "published_at": "now",
        }
        with db.connect() as conn:
            db.remember_seen_comment(
                conn, "old-comment", platform="instagram", status="posted"
            )
        with patch.object(webhook, "draft_reply") as draft, \
             patch.object(webhook.meta_client, "get_instagram_username", return_value="owner"):
            webhook.process_event(event)
        draft.assert_not_called()
        with db.connect() as conn:
            self.assertIsNone(db.get_comment(conn, "old-comment"))


if __name__ == "__main__":
    unittest.main()
