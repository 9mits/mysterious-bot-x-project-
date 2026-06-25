import unittest

from core.bot import fingerprint_payloads


class FingerprintPayloadsTests(unittest.TestCase):
    def test_same_commands_same_fingerprint(self):
        a = [{"name": "about", "description": "x"}, {"name": "stats", "description": "y"}]
        b = [{"name": "about", "description": "x"}, {"name": "stats", "description": "y"}]
        self.assertEqual(fingerprint_payloads(a), fingerprint_payloads(b))

    def test_order_independent(self):
        a = [{"name": "about"}, {"name": "stats"}, {"name": "directory"}]
        b = [{"name": "directory"}, {"name": "about"}, {"name": "stats"}]
        self.assertEqual(fingerprint_payloads(a), fingerprint_payloads(b))

    def test_added_command_changes_fingerprint(self):
        before = [{"name": "stats"}]
        after = [{"name": "stats"}, {"name": "about"}]
        self.assertNotEqual(fingerprint_payloads(before), fingerprint_payloads(after))

    def test_changed_description_changes_fingerprint(self):
        before = [{"name": "about", "description": "old"}]
        after = [{"name": "about", "description": "new"}]
        self.assertNotEqual(fingerprint_payloads(before), fingerprint_payloads(after))

    def test_empty_is_stable(self):
        self.assertEqual(fingerprint_payloads([]), fingerprint_payloads([]))


if __name__ == "__main__":
    unittest.main()
