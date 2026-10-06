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

    def test_odia_policy_converts_a_hindi_draft_to_odia(self):
        self.assertEqual(
            sanitize_draft("जय माँ दक्षिणकाली!", policy="odia_or_english"),
            "ଜୟ ମା ଦକ୍ଷିଣକାଳୀ!",
        )

    def test_odia_policy_drops_every_other_indic_script(self):
        # The guarantee odia_or_english exists for: nothing but Odia or Latin
        # survives, whatever language the draft came back in.
        self.assertEqual(
            sanitize_draft("ಧನ್ಯವಾದ Jay Maa", policy="odia_or_english"),
            "Jay Maa",
        )

    def test_match_commenter_keeps_a_kannada_reply(self):
        # Before the language policy existed this returned a bare 🙏: every
        # Kannada character was deleted as "not Odia" and the empty result hit
        # sanitize_draft's fallback.
        text = "ನಿಮ್ಮ ಮಾತು ಬಹಳ ಚೆನ್ನಾಗಿದೆ"
        self.assertEqual(sanitize_draft(text, policy="match_commenter"), text)

    def test_match_commenter_keeps_a_hindi_reply_as_hindi(self):
        text = "जय माँ दक्षिणकाली!"
        self.assertEqual(sanitize_draft(text, policy="match_commenter"), text)

    def test_match_commenter_still_transliterates_a_leak_into_odia(self):
        # An Odia reply with one Devanagari chant word left in it: the word is
        # transliterated rather than deleted, so the reply keeps its meaning.
        self.assertEqual(
            sanitize_draft("ଜୟ ମା ଦକ୍ଷିଣକାଳୀ जय", policy="match_commenter"),
            "ଜୟ ମା ଦକ୍ଷିଣକାଳୀ ଜୟ",
        )

    def test_match_commenter_drops_a_leak_into_a_non_odia_reply(self):
        # Majority script wins: the Kannada reply stays, the stray Devanagari
        # chant word goes. There is no Kannada transliteration table, and
        # mixing two scripts in one public reply is the thing being prevented.
        self.assertEqual(
            sanitize_draft("ಧನ್ಯವಾದ जय माँ", policy="match_commenter"),
            "ಧನ್ಯವಾದ",
        )

    def test_strips_glad_touched_heart_and_enjoyed_video(self):
        self.assertEqual(
            sanitize_draft(
                "Thanks for watching, and we're so glad this video touched your heart."
            ),
            "🙏",
        )
        self.assertEqual(
            sanitize_draft("Jay Jagannath! We're so glad you enjoyed the video."),
            "Jay Jagannath!",
        )

    def test_strips_odia_thanks_and_maa_krupa_closing(self):
        text = "ଜୟ ଜଗନ୍ନାଥ। ଆମ ଭିଡିଓ ଦେଖିଥିବାରୁ ବହୁତ ଧନ୍ୟବାଦ। ମା'ଙ୍କ କୃପା ସମସ୍ତଙ୍କୁ ଉପରେ ରହୁ।"
        self.assertEqual(sanitize_draft(text), "ଜୟ ଜଗନ୍ନାଥ")

    def test_keeps_mention_prefix(self):
        text = "@foo Thank you for watching and commenting on the video."
        self.assertEqual(sanitize_draft(text), "@foo 🙏")

    def test_removes_malformed_json_wrapper_after_mention(self):
        text = '@customername {"reply": ଜୟ ମା ଦକ୍ଷିଣକାଳୀ! }}'
        self.assertEqual(
            sanitize_draft(text),
            "@customername ଜୟ ମା ଦକ୍ଷିଣକାଳୀ!",
        )

    def test_removes_valid_json_wrapper_after_mention(self):
        text = '@customername {"reply": "🙏"}'
        self.assertEqual(sanitize_draft(text), "@customername 🙏")


if __name__ == "__main__":
    unittest.main()
