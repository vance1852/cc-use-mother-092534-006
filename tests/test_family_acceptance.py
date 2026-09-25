import unittest

from family_stories.acceptance import run


class FamilyAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("community", result["public_scope"])
        self.assertEqual("class", result["class_scope"])
        self.assertEqual("class", result["narrowed_scope"])
        self.assertTrue(result["names_hidden_at_class"])
        self.assertTrue(result["batch_replayed"])
        self.assertEqual(2, result["queue_size"])


if __name__ == "__main__":
    unittest.main()
