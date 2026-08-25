from __future__ import annotations

from unittest.mock import patch
import unittest

from chat_courier import liveness


class LivenessTests(unittest.TestCase):
    def test_invalid_pid_is_not_alive(self):
        self.assertFalse(liveness.process_alive(0))
        self.assertFalse(liveness.process_alive(-1))

    @unittest.skipUnless(liveness.os.name == "nt", "Windows-specific implementation")
    def test_windows_liveness_uses_read_only_handle_query(self):
        with patch("chat_courier.liveness._windows_process_alive", return_value=True) as probe:
            self.assertTrue(liveness.process_alive(1234))
        probe.assert_called_once_with(1234)
