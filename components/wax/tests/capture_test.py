import unittest
from unittest.mock import patch

from wax import capture


class CaptureTest(unittest.TestCase):
    def test_quiesce_is_successful_when_idle(self):
        with patch.object(capture.sentinel, "list_captures", return_value=[]):
            self.assertEqual(capture.quiesce(), {"action": "idle", "state": "ready"})

    def test_quiesce_stops_the_active_recording(self):
        rid = "20260822-133309-test"
        stopped = {"rid": rid, "path": "/tmp/recovered.ogg", "duration_s": 12.0}
        with patch.object(capture.sentinel, "list_captures", return_value=[rid]), \
                patch.object(capture.state, "stream_state", return_value={
                    "state": "recording", "rid": rid,
                }), \
                patch.object(capture, "stop", return_value=stopped) as stop:
            result = capture.quiesce()

        stop.assert_called_once_with(rid)
        self.assertEqual(result, {"action": "stopped", **stopped})

    def test_quiesce_never_automatically_salvages_an_error(self):
        rid = "20260822-133309-test"
        with patch.object(capture.sentinel, "list_captures", return_value=[rid]), \
                patch.object(capture.state, "stream_state", return_value={
                    "state": "error-partial", "cause_code": "uninstructed_exit", "rid": rid,
                }), \
                self.assertRaisesRegex(capture.CaptureError, "refusing automatic"):
            capture.quiesce()


if __name__ == "__main__":
    unittest.main()
