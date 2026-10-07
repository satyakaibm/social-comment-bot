import unittest

from app.dashboard import _safe_next


class SafeNextTests(unittest.TestCase):
    def test_accepts_same_origin_paths(self):
        for target in ("/", "/status", "/comments?status=pending&page=2", "/profile#photo"):
            with self.subTest(target=target):
                self.assertEqual(_safe_next(target), target)

    def test_rejects_empty_relative_and_absolute_urls(self):
        for target in ("", "status", "https://evil.com/", "javascript:alert(1)", "//evil.com", "///evil.com"):
            with self.subTest(target=target):
                self.assertEqual(_safe_next(target), "/")

    def test_rejects_backslash_variants_browsers_treat_as_scheme_relative(self):
        for target in ("/\\evil.com", "/\\/evil.com", "/\\\\evil.com", "/status\\..\\\\evil.com"):
            with self.subTest(target=target):
                self.assertEqual(_safe_next(target), "/")

    def test_rejects_control_characters(self):
        for target in ("/status\r\nSet-Cookie: a=b", "/status\x00", "/\tevil.com", "/status\x7f"):
            with self.subTest(target=target):
                self.assertEqual(_safe_next(target), "/")


if __name__ == "__main__":
    unittest.main()
