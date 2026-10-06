import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, reply_examples

from tests.helpers import make_page_config


class LoadExamplesTests(unittest.TestCase):
    def test_loads_pairs_from_the_page_persona_dir_and_skips_underscore_files(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "_reply_persona.txt").write_text("ignored persona", encoding="utf-8")
            (folder / "examples.txt").write_text(
                "comment: Best time to visit?\nreply: Go in winter.\n",
                encoding="utf-8",
            )
            page = make_page_config(key="travel", persona_dir=folder)
            with patch.object(config, "PAGES", {**config.PAGES, "travel": page}):
                pairs = reply_examples.load_examples(page_key="travel")

        self.assertEqual(pairs, [("Best time to visit?", "Go in winter.")])

    def test_format_for_prompt_follows_each_pages_language_policy(self):
        # The Odia-only line used to be hardcoded to the default page. It now
        # follows the page's REPLY_LANGUAGE_POLICY, so a page can keep the
        # strict rule while every other page mirrors the commenter instead.
        examples = [("hello", "🙏")]
        strict = make_page_config(
            key="strict", reply_language_policy="odia_or_english"
        )
        with patch.object(config, "PAGES", {**config.PAGES, "strict": strict}):
            strict_block = reply_examples.format_for_prompt(examples, page_key="strict")
        other_block = reply_examples.format_for_prompt(examples, page_key="travel")

        self.assertIn("only Odia or only English", strict_block)
        self.assertNotIn("only Odia or only English", other_block)
        self.assertIn("never their language", other_block)
        self.assertIn('Comment: "hello"', other_block)
        self.assertIn('Reply: "🙏"', other_block)
