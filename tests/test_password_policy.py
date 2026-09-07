import unittest

from app.password_policy import password_meets_policy


class PasswordPolicyTests(unittest.TestCase):
    def test_accepts_password_with_all_required_character_types(self):
        self.assertTrue(password_meets_policy("Secure1!"))

    def test_rejects_password_missing_each_requirement(self):
        for password in ("Short1!", "lowercase1!", "NoNumber!", "NoSpecial1"):
            with self.subTest(password=password):
                self.assertFalse(password_meets_policy(password))


if __name__ == "__main__":
    unittest.main()
