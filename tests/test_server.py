"""Regression tests never inject input into the user's desktop."""
import ctypes
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "tools" / "gui-control" / "server.py"
server = None
if sys.platform == "win32":
    spec = importlib.util.spec_from_file_location("gui_server", SERVER)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)


@unittest.skipUnless(sys.platform == "win32", "Windows GUI reference driver")
class ServerTests(unittest.TestCase):
    def exchange(self, lines):
        result = subprocess.run(
            [sys.executable, str(SERVER)], input="\n".join(lines) + "\n",
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()], result.stderr

    def test_bad_request_does_not_kill_server(self):
        replies, _ = self.exchange([
            "{", "[]",
            json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}),
        ])
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["error"]["code"], -32600)
        self.assertEqual(replies[2]["result"], {})
        self.assertEqual(replies[2]["id"], 7)

    def test_notifications_do_not_reply_or_inject_input(self):
        replies, _ = self.exchange([
            json.dumps({"jsonrpc": "2.0", "method": "tools/call",
                        "params": {"name": "press_key", "arguments": {"keys": "bogus"}}}),
            json.dumps({"jsonrpc": "2.0", "id": 8, "method": "ping"}),
        ])
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 8)

    def test_initialize_bom_and_read_only_tools(self):
        replies, _ = self.exchange([
            '\ufeff' + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2099-01-01"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ])
        self.assertEqual(replies[0]["result"]["protocolVersion"], server.PROTOCOL_VERSION)
        self.assertIn("screenshot_window", [tool["name"] for tool in replies[1]["result"]["tools"]])

    def test_window_enumeration_and_cursor_query_work_on_real_win32(self):
        # Read-only integration check, with no screenshots or desktop input.
        replies, _ = self.exchange([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "list_windows"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "screen_info"}}),
        ])
        self.assertFalse(replies[0]["result"]["isError"])
        self.assertIsInstance(json.loads(replies[0]["result"]["content"][0]["text"]), list)
        self.assertFalse(replies[1]["result"]["isError"])

    def test_invalid_window_is_a_tool_error(self):
        replies, _ = self.exchange([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "focus_window", "arguments": {"hwnd": 0}}}),
        ])
        self.assertTrue(replies[0]["result"]["isError"])

    def test_logs_omit_text_and_arguments(self):
        replies, logs = self.exchange([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "not-a-tool", "arguments": {"text": "secret-sentinel"}}}),
        ])
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertNotIn("secret-sentinel", logs)

    def test_vk_and_control_characters_have_real_virtual_keys(self):
        captured = []
        def send(events):
            captured.extend(events)
            return len(events)
        with patch.object(server, "_send_input", side_effect=send):
            server.type_text("\n\t")
            self.assertEqual([event.u.ki.wVk for event in captured], [13, 13, 9, 9])
            captured.clear()
            server.press_key("ctrl+s", mode="vk")
            self.assertEqual([event.u.ki.wVk for event in captured], [17, 83, 83, 17])
            self.assertTrue(all(event.u.ki.wScan == 0 for event in captured))

    def test_unicode_surrogates_and_scan_keys(self):
        captured = []
        def send(events):
            captured.extend(events)
            return len(events)
        with patch.object(server, "_send_input", side_effect=send):
            server.type_text("中😀")
            self.assertEqual([event.u.ki.wScan for event in captured[::2]], [0x4E2D, 0xD83D, 0xDE00])
            self.assertTrue(all(event.u.ki.wVk == 0 for event in captured))
            captured.clear()
            server.press_key("right")
            self.assertTrue(captured[0].u.ki.dwFlags & server.KEYEVENTF_SCANCODE)
            self.assertTrue(captured[0].u.ki.dwFlags & server.KEYEVENTF_EXTENDEDKEY)

    def test_input_failure_is_reported(self):
        with patch.object(server.user32, "SendInput", return_value=0):
            with self.assertRaises(OSError):
                server.type_text("a")

    def test_target_mismatch_refuses_input_without_moving_cursor(self):
        with patch.object(server.user32, "IsWindow", return_value=True), \
                patch.object(server.user32, "GetForegroundWindow", return_value=456), \
                patch.object(server.user32, "SetCursorPos") as cursor, \
                patch.object(server, "_send_input") as send:
            for name, arguments in (
                ("type_text", {"text": "private", "hwnd": 123}),
                ("press_key", {"keys": "enter", "hwnd": 123}),
                ("click", {"x": 10, "y": 10, "hwnd": 123}),
            ):
                with self.assertRaises(OSError):
                    server.dispatch(name, arguments)
            send.assert_not_called()
            cursor.assert_not_called()

    def test_click_rechecks_focus_after_cursor_move(self):
        with patch.object(server.user32, "IsWindow", return_value=True), \
                patch.object(server.user32, "GetForegroundWindow", side_effect=[123, 456]), \
                patch.object(server.user32, "WindowFromPoint", return_value=789), \
                patch.object(server.user32, "GetAncestor", return_value=123), \
                patch.object(server.user32, "SetCursorPos", return_value=True), \
                patch.object(server, "_send_input") as send:
            with self.assertRaises(OSError):
                server.click(10, 10, hwnd=123)
            send.assert_not_called()

    def test_click_outside_target_is_rejected(self):
        with patch.object(server.user32, "IsWindow", return_value=True), \
                patch.object(server.user32, "GetForegroundWindow", return_value=123), \
                patch.object(server.user32, "WindowFromPoint", return_value=456), \
                patch.object(server.user32, "GetAncestor", return_value=456), \
                patch.object(server.user32, "SetCursorPos") as cursor, \
                patch.object(server, "_send_input") as send:
            with self.assertRaises(OSError):
                server.click(10, 10, hwnd=123)
            send.assert_not_called()
            cursor.assert_not_called()

    def test_read_region_failure_is_mcp_tool_error(self):
        replies, _ = self.exchange([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "read_region", "arguments": {"image_path": "Z:/missing.png"}}}),
        ])
        self.assertTrue(replies[0]["result"]["isError"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "optional Pillow crop support")
    def test_read_region_integer_pixels_and_fractional_coordinates(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = Path(directory) / "crop.png"
            image = Image.new("RGB", (10, 10), "red")
            image.putpixel((1, 1), (0, 255, 0))
            image.save(source)
            image.close()
            result = json.loads(server.read_region(str(source), x=1, y=1, w=1, h=1,
                                                   scale=1, out_path=str(output)))
            self.assertEqual(result["region"], [1, 1, 2, 2])
            with Image.open(output) as crop:
                self.assertEqual(crop.size, (1, 1))
                self.assertEqual(crop.getpixel((0, 0)), (0, 255, 0))
            result = json.loads(server.read_region(str(source), x=0.5, y=0.5, w=0.5, h=0.5,
                                                   scale=1, out_path=str(output)))
            self.assertEqual(result["region"], [5, 5, 10, 10])
            for arguments in ({"scale": -1}, {"w": 0}, {"scale": float("nan")}):
                with self.assertRaises(ValueError):
                    server.read_region(str(source), **arguments)

    def test_screenshot_deselects_bitmap_and_cleans_up_on_readback_failure(self):
        def rect(hwnd, output):
            target = ctypes.cast(output, ctypes.POINTER(server.RECT)).contents
            target.right, target.bottom = 2, 2
            return 1
        selection = Mock(return_value=4)
        def readback(*args):
            self.assertEqual(selection.call_args_list, [call(2, 3), call(2, 4)])
            return 0
        with patch.multiple(server.user32,
                            IsWindow=Mock(return_value=True), IsIconic=Mock(return_value=False),
                            GetWindowRect=Mock(side_effect=rect), GetWindowDC=Mock(return_value=1),
                            PrintWindow=Mock(return_value=True), ReleaseDC=Mock()) as _:
            with patch.multiple(server.gdi32,
                                CreateCompatibleDC=Mock(return_value=2),
                                CreateCompatibleBitmap=Mock(return_value=3),
                                SelectObject=selection, GetDIBits=Mock(side_effect=readback),
                                DeleteObject=Mock(), DeleteDC=Mock()):
                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "failed.png"
                    with self.assertRaises(OSError):
                        server.screenshot_window(123, str(output))
                    self.assertFalse(output.exists())
                server.gdi32.DeleteObject.assert_called_once_with(3)
                server.gdi32.DeleteDC.assert_called_once_with(2)
                self.assertEqual(server.user32.ReleaseDC.call_count, 1)

    def test_pointer_width_and_handle_signatures(self):
        self.assertEqual(ctypes.sizeof(server.KEYBDINPUT), 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)
        self.assertEqual(server.user32.GetWindowDC.restype, ctypes.wintypes.HDC)
        self.assertEqual(server.user32.GetForegroundWindow.restype, ctypes.wintypes.HWND)
        self.assertEqual(server.gdi32.SelectObject.restype, ctypes.wintypes.HANDLE)

    def test_png_pixels_are_interleaved_and_top_down(self):
        # Two rows, red/green/blue then blue/green/red, with alpha ignored.
        bgra = bytes([0, 0, 255, 255, 0, 255, 0, 255, 255, 0, 0, 255,
                      255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255])
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "test.png"
            server._write_png(image, 3, 2, bgra)
            png = image.read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        offset = 8
        compressed = b""
        while offset < len(png):
            size = struct.unpack(">I", png[offset:offset + 4])[0]
            tag = png[offset + 4:offset + 8]
            data = png[offset + 8:offset + 8 + size]
            if tag == b"IDAT":
                compressed += data
            offset += 12 + size
        self.assertEqual(zlib.decompress(compressed),
                         bytes([0, 255, 0, 0, 0, 255, 0, 0, 0, 255,
                                0, 0, 0, 255, 0, 255, 0, 255, 0, 0]))


if __name__ == "__main__":
    unittest.main()
