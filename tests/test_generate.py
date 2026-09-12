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


class GenerateTests(unittest.TestCase):
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

    def test_only_instagram_drafts_receive_an_author_prefix(self):
        client = SimpleNamespace(chats=_FakeChats())
        with patch.object(generate, "_get_client", return_value=client), \
             patch.object(generate, "load_examples", return_value=[]):
            youtube = generate.draft_reply(
                platform="youtube", context_title="Aarti", author="viewer",
                comment_text="Jai Maa",
            )
            instagram = generate.draft_reply(
                platform="instagram", context_title="Aarti", author="viewer",
                comment_text="Jai Maa",
            )

        self.assertEqual(youtube, "🙏")
        self.assertEqual(instagram, "@viewer 🙏")

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
        self.assertNotIn("hindolroad / Hindolroad", system)
