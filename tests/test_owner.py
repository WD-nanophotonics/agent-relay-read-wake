from __future__ import annotations

import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from chat_courier.owner import OwnerBusy, OwnerLease, read_owner


class OwnerTests(unittest.TestCase):
    def test_live_owner_blocks_second_owner_and_releases_cleanly(self):
        with tempfile.TemporaryDirectory() as value, \
                patch("chat_courier.owner.runtime_root", return_value=Path(value)):
            first = OwnerLease("P", "P-1")
            second = OwnerLease("P", "P-2")
            first.acquire("test")
            try:
                self.assertEqual(read_owner().request_id, "P-1")
                with self.assertRaises(OwnerBusy):
                    second.acquire("test")
            finally:
                first.release()
            second.acquire("test")
            second.release()
            self.assertIsNone(read_owner())
