import unittest

from app.sanitize import sanitize_draft


class SanitizeDraftTests(unittest.TestCase):
    def test_strips_english_thanks_for_watching(self):
        text = "Jay Maa! Thank you for watching and sharing your devotion with us! ❤️"
        self.assertEqual(sanitize_draft(text), "Jay Maa!")

    def test_strips_video_shares_filler(self):
        text = "ଜୟ ମା ଦକ୍ଷିଣକାଳୀ! Our video shares the daily rituals of the Goddess, and we hope you enjoyed watching it."
        self.assertEqual(sanitize_draft(text), "ଜୟ ମା ଦକ୍ଷିଣକାଳୀ!")

    def test_strips_odia_thanks_for_watching(self):
        text = "ଜୟ ମା' ବାଟ ମଙ୍ଗଳା! ଆମ ଭିଡିଓ ଦେଖିଥିବାରୁ ଏବଂ କମେଣ୍ଟ କରିଥିବାରୁ ଆପଣଙ୍କୁ ଅନେକ ଧନ୍ୟବାଦ।"
        self.assertEqual(sanitize_draft(text), "ଜୟ ମା' ବାଟ ମଙ୍ଗଳା!")

    def test_converts_hindi_jay_to_odia(self):
        self.assertEqual(sanitize_draft("जय माँ दक्षिणकाली!"), "ଜୟ ମା ଦକ୍ଷିଣକାଳୀ!")

    def test_keeps_mention_prefix(self):
        text = "@foo Thank you for watching and commenting on the video."
        self.assertEqual(sanitize_draft(text), "@foo 🙏")


if __name__ == "__main__":
    unittest.main()
