import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/gui-control/selftest.py"
spec = importlib.util.spec_from_file_location("desktop_selftest", SCRIPT)
selftest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selftest)


class DelayedFixture:
    """Each injected event needs more than one snapshot to become visible."""
    def __init__(self):
        self.phase = "focus"
        self.polls = 0
        self.events = []
        self.state = {"text": "", "entry": "", "enters": 0, "clicks": 0, "button": [1, 2]}

    def tool(self, name, arguments):
        self.events.append(name)
        self.polls = 0
        if name == "type_text":
            self.phase = "text" if "\n" in arguments["text"] else "entry"
        elif name == "press_key":
            self.phase = arguments["keys"]
        else:
            self.phase = name
        return "accepted"

    def send(self, message):
        self.polls += 1
        if self.polls >= 2:
            if self.phase == "text":
                self.state["text"] = "GUI 验收 😀\nsecond line"
            elif self.phase == "entry":
                self.state["entry"] = "tab landed"
            elif self.phase == "enter":
                self.state["enters"] = 1
            elif self.phase == "click":
                self.state["clicks"] = 1
        return dict(self.state)


class SelftestTests(unittest.TestCase):
    def test_notice_failure_stops_before_any_gui_work(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "check"
            with patch.object(selftest, "notify_takeover", return_value=1), \
                    patch.object(selftest, "win32") as native, patch("builtins.print"):
                self.assertEqual(selftest.run(output), 1)
            native.assert_not_called()
            self.assertFalse(output.exists())

    def test_existing_output_does_not_show_notice_or_modify_files(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(selftest, "notify_takeover") as notice:
                with self.assertRaises(ValueError):
                    selftest.run(output)
            notice.assert_not_called()

    def test_notice_launches_the_packaged_script_and_preserves_cancellation(self):
        with patch.object(selftest.subprocess, "run", return_value=Mock(returncode=2)) as process:
            self.assertEqual(selftest.notify_takeover(), 2)
        command = process.call_args.args[0]
        self.assertEqual(command[0], "powershell.exe")
        self.assertEqual(Path(command[3]), SCRIPT.parent.parent / "notify-takeover.ps1")
        self.assertEqual(process.call_args.kwargs["creationflags"], selftest.subprocess.CREATE_NO_WINDOW)

    def test_cancelled_notice_does_not_open_fixture_or_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "check"
            with patch.object(selftest, "notify_takeover", return_value=2), \
                    patch.object(selftest, "win32", side_effect=AssertionError("GUI before notice")) as native, \
                    patch("builtins.print") as print_result:
                self.assertEqual(selftest.run(output), 2)
            native.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(json.loads(print_result.call_args.args[0])["status"], "cancelled")

    def test_native_input_waits_for_observed_state_without_reinjecting(self):
        fixture = DelayedFixture()
        report, checks = {}, []
        with patch.object(selftest.time, "sleep"):
            self.assertEqual(selftest._input_checks(fixture, fixture, 123, checks, report), 0)
        self.assertEqual(report["native_state"]["clicks"], 1)
        self.assertEqual(report["native_state"]["enters"], 1)
        self.assertEqual(fixture.events, ["focus_window", "type_text", "press_key", "type_text", "press_key", "click"])

    def test_timeout_reports_observed_state_and_does_not_send_next_input(self):
        driver = Mock()
        ui = Mock()
        ui.send.return_value = {"text": "incomplete"}
        report = {}
        with patch.object(selftest.time, "monotonic", side_effect=[0, 4]):
            self.assertEqual(selftest._input_checks(driver, ui, 123, [], report), 1)
        self.assertIn("timed out", report["error"])
        self.assertEqual([c.args[0] for c in driver.tool.call_args_list], ["focus_window", "type_text"])


if __name__ == "__main__":
    unittest.main()
