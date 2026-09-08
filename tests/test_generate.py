import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import generate


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
