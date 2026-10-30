import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import config, generate

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


class _FakeClaudeMessages:
    def __init__(self, text='{"language": "Odia", "reply": "🙏"}', stop_reason="end_turn"):
        self.calls = []
        self.text = text
        self.stop_reason = stop_reason

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            stop_details=None,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=self.text),
            ],
        )


def _fake_claude(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


class GenerateTests(unittest.TestCase):
    def test_claude_provider_drafts_through_anthropic(self):
        messages = _FakeClaudeMessages()
        with patch.object(config, "LLM_PROVIDER", "claude"), \
             patch.object(generate, "_get_claude_client", return_value=_fake_claude(messages)), \
             patch.object(generate, "_get_client", side_effect=AssertionError("Gemini used")), \
             patch.object(generate, "load_examples", return_value=[]):
            reply = generate.draft_reply(
                platform="instagram", context_title="Aarti", author="viewer",
                comment_text="Jai Maa",
            )

        self.assertEqual(reply, "@viewer 🙏")
        call = messages.calls[0]
        self.assertEqual(call["model"], config.CLAUDE_MODEL)
        self.assertIn("Jai Maa", call["messages"][0]["content"])
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")

    def test_claude_refusal_raises_instead_of_posting_empty_reply(self):
        messages = _FakeClaudeMessages(text="", stop_reason="refusal")
        with patch.object(config, "LLM_PROVIDER", "claude"), \
             patch.object(generate, "_get_claude_client", return_value=_fake_claude(messages)), \
             patch.object(generate, "load_examples", return_value=[]):
            with self.assertRaises(RuntimeError):
                generate.draft_reply(
                    platform="youtube", context_title="Aarti", author="viewer",
                    comment_text="Jai Maa",
                )

    def test_draft_uses_chat_send_message(self):
        client = SimpleNamespace(chats=_FakeChats())
        with patch.object(generate, "_get_client", return_value=client), \
             patch.object(generate, "load_examples", return_value=[]):
            reply = generate.draft_reply(
                platform="facebook",
                context_title="Aarti",
                author="viewer",
                comment_text="Jai Maa",
            )

        self.assertEqual(reply, "🙏")
        self.assertEqual(len(client.chats.created), 1)
        self.assertIn("Jai Maa", client.chats.chat.messages[0])

    def _draft(self, platform, author):
        client = SimpleNamespace(chats=_FakeChats())
        with patch.object(generate, "_get_client", return_value=client), \
             patch.object(generate, "load_examples", return_value=[]):
            reply = generate.draft_reply(
                platform=platform, context_title="Aarti", author=author,
                comment_text="Jai Maa",
            )
        return reply, client.chats.chat.messages[0]

    def test_instagram_and_youtube_handles_become_mentions(self):
        self.assertEqual(self._draft("instagram", "viewer")[0], "@viewer 🙏")
        # YouTube's authorDisplayName already starts with "@"; it must not
        # gain a second one.
        self.assertEqual(self._draft("youtube", "@viewer-12")[0], "@viewer-12 🙏")

    def test_facebook_and_non_handle_youtube_names_stay_plain(self):
        self.assertEqual(self._draft("facebook", "Rajesh Kumar Dash")[0], "🙏")
        self.assertEqual(self._draft("youtube", "Rajesh Dash")[0], "🙏")

    def test_second_page_uses_its_persona_and_generic_style_not_hindolroad_rules(self):
        second = make_page_config(
            key="travel",
            persona="You are a witty travel community manager.",
        )
        client = SimpleNamespace(chats=_FakeChats())
        with patch.object(config, "PAGES", {**config.PAGES, "travel": second}), \
             patch.object(generate, "_get_client", return_value=client), \
             patch.object(generate, "load_examples", return_value=[]):
            reply = generate.draft_reply(
                platform="youtube",
                page_key="travel",
                context_title="Goa",
                author="viewer",
                comment_text="When to visit?",
            )

        self.assertEqual(reply, "🙏")
        system = client.chats.created[0]["config"].system_instruction
        self.assertIn("witty travel community manager", system)
        self.assertNotIn("This channel is Hindolroad", system)
        self.assertNotIn("only 🙏 for devotional chants", system)
