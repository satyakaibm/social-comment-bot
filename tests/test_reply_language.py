"""Per-page reply-language policy: the prompt rules, the sanitizer's choice of
script, and the gate that holds an unreviewed draft in a new script.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import config, db, generate, meta_client, post, reply_examples

from tests.helpers import make_page_config


class _FakeChat:
    def __init__(self):
        self.messages = []

    def send_message(self, message):
        self.messages.append(message)
        return SimpleNamespace(text='{"reply": "🙏"}')


class _FakeChats:
    def __init__(self):
        self.created = []
        self.chat = _FakeChat()

    def create(self, **kwargs):
        self.created.append(kwargs)
        return self.chat


class LanguagePolicyConfigTests(unittest.TestCase):
    def test_policy_defaults_to_matching_the_commenter(self):
        self.assertEqual(config.DEFAULT_REPLY_LANGUAGE_POLICY, "match_commenter")
        self.assertEqual(
            config.reply_language_policy(config.DEFAULT_PAGE_KEY), "match_commenter"
        )

    def test_unknown_page_key_falls_back_instead_of_raising(self):
        # Comment rows written before multi-page support store page_key='',
        # and a tenant's rows can outlive its PAGES entry.
        self.assertEqual(config.reply_language_policy(""), "match_commenter")
        self.assertEqual(config.reply_language_policy("gone"), "match_commenter")
        self.assertEqual(config.reply_auto_post_scripts("gone"), ())

    def test_invalid_policy_is_rejected_at_parse_time(self):
        with patch.dict("os.environ", {"REPLY_LANGUAGE_POLICY_2": "hinglish"}):
            with self.assertRaises(RuntimeError) as caught:
                config._reply_language_policy("_2")
        self.assertIn("match_commenter", str(caught.exception))

    def test_auto_post_scripts_parse_case_insensitively(self):
        with patch.dict("os.environ", {"REPLY_AUTO_POST_SCRIPTS": " odia , latin "}):
            self.assertEqual(config._reply_auto_post_scripts(""), ("Odia", "Latin"))

    def test_unknown_script_name_is_rejected_at_parse_time(self):
        with patch.dict("os.environ", {"REPLY_AUTO_POST_SCRIPTS": "Odiya"}):
            with self.assertRaises(RuntimeError) as caught:
                config._reply_auto_post_scripts("")
        self.assertIn("Odiya", str(caught.exception))


class LanguageInstructionTests(unittest.TestCase):
    def test_match_commenter_asks_for_the_commenters_language(self):
        block = generate._style_instruction(config.DEFAULT_PAGE_KEY)
        self.assertIn("same language AND the same script", block)
        self.assertIn("Kannada to Kannada", block)
        self.assertNotIn("either Odia or English", block)

    def test_odia_policy_keeps_the_original_hindolroad_rules(self):
        page = make_page_config(
            key="hindolroad", reply_language_policy="odia_or_english"
        )
        with patch.object(config, "PAGES", {**config.PAGES, "hindolroad": page}):
            block = generate._style_instruction("hindolroad")
        self.assertIn("either Odia or English", block)
        self.assertIn("Do not use Hindi, Gujarati, Bengali", block)
        self.assertNotIn("Kannada to Kannada", block)

    def test_style_rules_other_than_language_survive_the_policy_switch(self):
        # The no-generic-thanks rules are about tone, not language, so they
        # must appear under either policy.
        for policy in config.REPLY_LANGUAGE_POLICIES:
            page = make_page_config(key="hindolroad", reply_language_policy=policy)
            with patch.object(config, "PAGES", {**config.PAGES, "hindolroad": page}):
                block = generate._style_instruction("hindolroad")
            self.assertIn("Never add generic thanks", block, policy)

    def test_fallback_language_is_only_named_when_configured(self):
        without = make_page_config(key="page")
        with_fallback = make_page_config(key="page", reply_fallback_language="Odia")
        with patch.object(config, "PAGES", {**config.PAGES, "page": without}):
            self.assertNotIn("reply in Odia", generate._style_instruction("page"))
        with patch.object(config, "PAGES", {**config.PAGES, "page": with_fallback}):
            self.assertIn("reply in Odia", generate._style_instruction("page"))

    def test_every_policy_has_an_instruction_block(self):
        for policy in config.REPLY_LANGUAGE_POLICIES:
            self.assertIn(policy, generate._LANGUAGE_INSTRUCTIONS)

    def test_tenant_prompt_carries_the_language_rules_too(self):
        # The whole point of the rollout: a tenant page gets the same
        # language handling as the main instance, not just hindolroad.
        tenant = make_page_config(key="travel", persona="Witty travel manager.")
        client = SimpleNamespace(chats=_FakeChats())
        with patch.object(config, "PAGES", {**config.PAGES, "travel": tenant}), \
             patch.object(generate, "_get_client", return_value=client), \
             patch.object(generate, "load_examples", return_value=[]):
            generate.draft_reply(
                platform="youtube",
                page_key="travel",
                context_title="Goa",
                author="viewer",
                comment_text="ಯಾವಾಗ ಹೋಗಬೇಕು?",
            )
        system = client.chats.created[0]["config"].system_instruction
        self.assertIn("same language AND the same script", system)
        self.assertIn("Witty travel manager.", system)


class ExamplesIntroTests(unittest.TestCase):
    def test_match_commenter_tells_the_model_to_ignore_the_examples_language(self):
        intro = reply_examples.format_for_prompt(
            [("hello", "🙏")], page_key=config.DEFAULT_PAGE_KEY
        )
        self.assertIn("never their language", intro)
        self.assertNotIn("only Odia or only English", intro)

    def test_odia_policy_still_states_the_odia_only_rule(self):
        page = make_page_config(key="hindolroad", reply_language_policy="odia_or_english")
        with patch.object(config, "PAGES", {**config.PAGES, "hindolroad": page}):
            intro = reply_examples.format_for_prompt(
                [("hello", "🙏")], page_key="hindolroad"
            )
        self.assertIn("only Odia or only English", intro)


class AutoPostScriptGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self.temp.name) / "comments.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()

    def seed(self, draft, *, status="pending_review"):
        with db.connect() as conn:
            db.insert_comment(
                conn,
                comment_id="c1",
                platform="facebook",
                video_id="media",
                video_title="Title",
                author="viewer",
                text="ಚೆನ್ನಾಗಿದೆ",
                published_at="",
                draft_reply=draft,
            )
            if status != "pending_review":
                db.update_status(conn, "c1", status)

    def _post(self, scripts):
        page = make_page_config(
            key=config.DEFAULT_PAGE_KEY, reply_auto_post_scripts=scripts
        )
        with patch.object(config, "PAGES", {config.DEFAULT_PAGE_KEY: page}), \
             patch.object(post.config, "FACEBOOK_VERIFY_EXISTING_REPLIES", False), \
             patch.object(meta_client, "find_own_reply"), \
             patch.object(meta_client, "reply_to_comment", return_value="new") as send:
            posted = post.post_approved(platform="facebook", include_pending=True)
        return posted, send

    def row(self):
        with db.connect() as conn:
            return conn.execute(
                "SELECT * FROM comments WHERE comment_id='c1'"
            ).fetchone()

    def test_draft_in_an_ungated_script_is_held_for_review(self):
        self.seed("ನಿಮ್ಮ ಮಾತು ಚೆನ್ನಾಗಿದೆ")
        posted, send = self._post(("Odia", "Latin"))
        self.assertEqual(posted, 0)
        send.assert_not_called()
        row = self.row()
        self.assertEqual(row["status"], "pending_review")
        self.assertIn("Kannada", row["error"])

    def test_draft_in_an_allowed_script_posts(self):
        self.seed("ଜୟ ମା")
        posted, send = self._post(("Odia", "Latin"))
        self.assertEqual(posted, 1)
        send.assert_called_once()

    def test_no_gate_configured_posts_every_language(self):
        self.seed("ನಿಮ್ಮ ಮಾತು ಚೆನ್ನಾಗಿದೆ")
        posted, send = self._post(())
        self.assertEqual(posted, 1)
        send.assert_called_once()

    def test_an_approved_draft_is_never_held(self):
        # A person already read this one on the dashboard; holding it again
        # would strand it in pending_review forever.
        self.seed("ನಿಮ್ಮ ಮಾತು ಚೆನ್ನಾಗಿದೆ", status="approved")
        posted, send = self._post(("Odia", "Latin"))
        self.assertEqual(posted, 1)
        send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
