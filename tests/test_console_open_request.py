"""A launcher the operator clicks opens the console tab; an unattended restart does not."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.test_console import fleetctl


class ConsoleOpenRequest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.marker = self.state / fleetctl.CONSOLE_OPEN_REQUEST

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_request_means_no_browser(self):
        self.assertFalse(fleetctl.consume_console_open_request(self.state))

    def test_fresh_request_opens_once(self):
        self.marker.write_text("")
        self.assertTrue(fleetctl.consume_console_open_request(self.state))
        self.assertFalse(self.marker.exists())
        self.assertFalse(fleetctl.consume_console_open_request(self.state))

    def test_stale_request_is_removed_without_opening(self):
        self.marker.write_text("")
        old = time.time() - fleetctl.CONSOLE_OPEN_REQUEST_MAX_AGE_S - 60
        os.utime(self.marker, (old, old))
        self.assertFalse(fleetctl.consume_console_open_request(self.state))
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
