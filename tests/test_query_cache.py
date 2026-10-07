import threading
import time
import unittest
from unittest.mock import patch

from app import config, query_cache


class QueryCacheTests(unittest.TestCase):
    def setUp(self):
        query_cache.clear()

    def test_reuses_value_within_ttl_and_recomputes_after(self):
        calls = []
        compute = lambda: calls.append(1) or [{"n": len(calls)}]
        first = query_cache.cached("k", compute, ttl=0.2)
        second = query_cache.cached("k", compute, ttl=0.2)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        time.sleep(0.25)
        query_cache.cached("k", compute, ttl=0.2)
        self.assertEqual(len(calls), 2)

    def test_callers_get_independent_copies(self):
        rows = query_cache.cached("rows", lambda: [{"page_key": ""}], ttl=60)
        rows[0]["page_key"] = "mutated"
        again = query_cache.cached("rows", lambda: self.fail("recomputed"), ttl=60)
        self.assertEqual(again, [{"page_key": ""}])

    def test_zero_ttl_disables_caching(self):
        calls = []
        with patch.object(config, "ANALYTICS_CACHE_SECONDS", 0):
            for _ in range(3):
                query_cache.cached("off", lambda: calls.append(1))
        self.assertEqual(len(calls), 3)
        self.assertEqual(query_cache.size(), 0)

    def test_default_ttl_comes_from_config(self):
        calls = []
        with patch.object(config, "ANALYTICS_CACHE_SECONDS", 60):
            query_cache.cached("cfg", lambda: calls.append(1))
            query_cache.cached("cfg", lambda: calls.append(1))
        self.assertEqual(len(calls), 1)

    def test_concurrent_misses_compute_once(self):
        calls = []
        started = threading.Barrier(5)

        def slow():
            calls.append(1)
            time.sleep(0.1)
            return "v"

        def worker():
            started.wait()
            query_cache.cached("slow", slow, ttl=60)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1)

    def test_clear_forgets_everything(self):
        query_cache.cached("a", lambda: 1, ttl=60)
        query_cache.clear()
        self.assertEqual(query_cache.size(), 0)

    def test_eviction_keeps_table_bounded(self):
        for i in range(query_cache._MAX_ENTRIES + 20):
            query_cache.cached(("many", i), lambda: i, ttl=60)
        self.assertLessEqual(query_cache.size(), query_cache._MAX_ENTRIES)


if __name__ == "__main__":
    unittest.main()
