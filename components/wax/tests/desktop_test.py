import unittest
from unittest.mock import patch

from wax import desktop


class DesktopNotificationTest(unittest.TestCase):
    @patch("wax.desktop.subprocess.Popen")
    @patch("wax.desktop.shutil.which", return_value="/usr/bin/canberra-gtk-play")
    def test_start_and_complete_are_audible_desktop_events(self, _which, popen):
        self.assertTrue(desktop.ding("start"))
        self.assertTrue(desktop.ding("complete"))
        self.assertEqual(popen.call_args_list[0].args[0][2], "message")
        self.assertEqual(popen.call_args_list[1].args[0][2], "complete")
