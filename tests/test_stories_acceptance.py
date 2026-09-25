import unittest

from festival_foundation.stories_acceptance import run


class StoriesAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(2, result["current_version"])
        self.assertTrue(result["community_address_replaced"])
        self.assertTrue(result["narrowed_hidden"])
        self.assertTrue(result["old_opinions_kept"])
        self.assertTrue(result["import_replayed"])


if __name__ == "__main__":
    unittest.main()
