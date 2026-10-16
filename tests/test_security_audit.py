from __future__ import annotations

import os
import stat
import tempfile
import unittest

from tools.audit_privacy import ResourceParser, safe_member
from wx4.security import private_dir, private_file


class PrivacyAuditTests(unittest.TestCase):
    def test_remote_click_link_is_allowed_but_remote_resource_is_rejected(self):
        parser = ResourceParser()
        parser.feed("<a href='https://example.com'>链接</a><img src='https://bad.test/a.png'>")
        self.assertEqual(parser.remote, ["https://bad.test/a.png"])

    def test_zip_member_guard_and_private_modes(self):
        self.assertTrue(safe_member("media/a.mp3"))
        self.assertFalse(safe_member("../keys.json"))
        self.assertFalse(safe_member("/etc/passwd"))
        with tempfile.TemporaryDirectory() as temp:
            directory = os.path.join(temp, "private")
            private_dir(directory)
            path = os.path.join(directory, "data.txt")
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("private")
            private_file(path)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
