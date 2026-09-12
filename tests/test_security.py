import unittest
from unittest.mock import patch

from app import config


class RuntimeSecurityTests(unittest.TestCase):
    def test_insecure_local_allows_dev_defaults(self):
        with patch.object(config, "DASHBOARD_INSECURE_LOCAL", True), \
             patch.object(config, "DASHBOARD_SECRET", "localhost-dashboard"), \
             patch.object(config, "DASHBOARD_COOKIE_SECURE", False), \
             patch.object(config, "DB_ENCRYPTION_KEY", ""):
            config.validate_runtime_security()

    def test_production_rejects_default_secret(self):
        with patch.object(config, "DASHBOARD_INSECURE_LOCAL", False), \
             patch.object(config, "DASHBOARD_SECRET", "localhost-dashboard"), \
             patch.object(config, "DASHBOARD_COOKIE_SECURE", True), \
             patch.object(config, "DB_ENCRYPTION_KEY", "k" * 32):
            with self.assertRaisesRegex(RuntimeError, "DASHBOARD_SECRET"):
                config.validate_runtime_security()

    def test_production_rejects_insecure_cookies(self):
        with patch.object(config, "DASHBOARD_INSECURE_LOCAL", False), \
             patch.object(config, "DASHBOARD_SECRET", "s" * 32), \
             patch.object(config, "DASHBOARD_COOKIE_SECURE", False), \
             patch.object(config, "DB_ENCRYPTION_KEY", "k" * 32):
            with self.assertRaisesRegex(RuntimeError, "DASHBOARD_COOKIE_SECURE"):
                config.validate_runtime_security()

    def test_production_rejects_missing_db_key(self):
        with patch.object(config, "DASHBOARD_INSECURE_LOCAL", False), \
             patch.object(config, "DASHBOARD_SECRET", "s" * 32), \
             patch.object(config, "DASHBOARD_COOKIE_SECURE", True), \
             patch.object(config, "DB_ENCRYPTION_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "DB_ENCRYPTION_KEY"):
                config.validate_runtime_security()

    def test_production_accepts_hardened_settings(self):
        with patch.object(config, "DASHBOARD_INSECURE_LOCAL", False), \
             patch.object(config, "DASHBOARD_SECRET", "s" * 32), \
             patch.object(config, "DASHBOARD_COOKIE_SECURE", True), \
             patch.object(config, "DB_ENCRYPTION_KEY", "k" * 32):
            config.validate_runtime_security()
