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

    def test_format_for_prompt_keeps_hindolroad_language_rule_off_other_pages(self):
        examples = [("hello", "🙏")]
        default_block = reply_examples.format_for_prompt(
            examples, page_key=config.DEFAULT_PAGE_KEY
        )
        other_block = reply_examples.format_for_prompt(examples, page_key="travel")

        self.assertIn("only Odia or only English", default_block)
        self.assertNotIn("only Odia or only English", other_block)
        self.assertIn('Comment: "hello"', other_block)
        self.assertIn('Reply: "🙏"', other_block)
