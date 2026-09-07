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
        like.assert_called_once_with("comment")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT status, reply_comment_id FROM comments WHERE comment_id='comment'"
            ).fetchone()
        self.assertEqual((row["status"], row["reply_comment_id"]), ("posted", "reply"))


if __name__ == "__main__":
    unittest.main()
