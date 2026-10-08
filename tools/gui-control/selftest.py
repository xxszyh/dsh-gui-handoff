"""Opt-in Windows desktop acceptance against a disposable, owned Tk window.

Run: python tools/gui-control/selftest.py --output .local/desktop-check
This opens a visible fixture briefly. It never selects a window by title.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
import time
import zlib


def win32():
    dll = ctypes.WinDLL("user32", use_last_error=True)
    for name, arguments, result in [
        ("GetAncestor", [wt.HWND, wt.UINT], wt.HWND),
        ("GetForegroundWindow", [], wt.HWND),
        ("SetForegroundWindow", [wt.HWND], wt.BOOL),
        ("IsWindow", [wt.HWND], wt.BOOL),
        ("AllowSetForegroundWindow", [wt.DWORD], wt.BOOL),
        ("SetCursorPos", [ctypes.c_int, ctypes.c_int], wt.BOOL),
        ("GetCursorPos", [ctypes.POINTER(wt.POINT)], wt.BOOL),
    ]:
        function = getattr(dll, name)
        function.argtypes, function.restype = arguments, result
    dll.SetProcessDPIAware()
    return dll


def notify_takeover():
    """The owned-window check obeys the same cancellation gate as other callers."""
    script = Path(__file__).resolve().parent.parent / "notify-takeover.ps1"
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(script), "-Task",
         "GUI driver acceptance in a disposable test window", "-Steps", "5", "-Seconds", "3"],
        creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, text=True,
    ).returncode


def fixture():
    import tkinter as tk
    dll = win32()
    root = tk.Tk()
    root.title("dsh-gui-handoff disposable acceptance fixture")
    root.geometry("640x380+80+80")
    root.resizable(False, False)
    tk.Label(root, text="GUI acceptance fixture — this window closes automatically",
             font=("Segoe UI", 12)).pack(pady=8)
    canvas = tk.Canvas(root, width=270, height=60, highlightthickness=0)
    canvas.pack()
    for i, color in enumerate(("#ff0000", "#00ff00", "#0000ff")):
        canvas.create_rectangle(i * 90, 0, (i + 1) * 90, 60, fill=color, width=0)
    text = tk.Text(root, height=5, width=45, font=("Segoe UI", 12))
    text.pack(pady=8)
    entry = tk.Entry(root, width=45, font=("Segoe UI", 12))
    entry.pack()
    counters = {"clicks": 0, "enters": 0}
    def increment(field):
        counters[field] += 1
        status.config(text=json.dumps(counters))
    button = tk.Button(root, text="Fixture button", command=lambda: increment("clicks"))
    button.pack(pady=8)
    status = tk.Label(root, text="Waiting for checks")
    status.pack()
    text.bind("<Tab>", lambda event: (entry.focus_set(), "break")[1])
    entry.bind("<Return>", lambda event: (increment("enters"), "break")[1])
    root.update()
    hwnd = dll.GetAncestor(root.winfo_id(), 2)
    if not args.capture_only:
        text.focus_force()
    root.after(250, lambda: print(json.dumps({"pid": os.getpid(), "hwnd": hwnd,
                                            "foreground": dll.GetForegroundWindow()}), flush=True))

    def command(message):
        try:
            action = message["action"]
            if action == "allow":
                result = {"allowed": bool(dll.AllowSetForegroundWindow(message["pid"])),
                          "foreground": dll.GetForegroundWindow()}
            elif action == "snapshot":
                result = {"text": text.get("1.0", "end-1c"), "entry": entry.get(),
                          **counters,
                          "button": [button.winfo_rootx() + button.winfo_width() // 2,
                                     button.winfo_rooty() + button.winfo_height() // 2],
                          "color_points": [[canvas.winfo_rootx() + 45 + 90 * i,
                                            canvas.winfo_rooty() + 30] for i in range(3)]}
            elif action == "close":
                print(json.dumps({"closed": True}), flush=True)
                root.destroy()
                return
            else:
                raise ValueError("unknown fixture command")
            print(json.dumps(result, ensure_ascii=True), flush=True)
        except Exception as error:
            print(json.dumps({"error": str(error)}), flush=True)
    def reader():
        for line in sys.stdin:
            message = json.loads(line)
            root.after(0, command, message)
    threading.Thread(target=reader, daemon=True).start()
    root.after(60000, root.destroy)
    root.mainloop()


class Peer:
    def __init__(self, command):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        self.messages = queue.Queue()
        self.logs = []
        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        def drain():
            self.logs.extend(self.process.stderr.readlines())
        threading.Thread(target=read, daemon=True).start()
        self.log_thread = threading.Thread(target=drain, daemon=True)
        self.log_thread.start()
        self.sequence = 0

    def receive(self):
        try:
            return self.messages.get(timeout=10)
        except queue.Empty as error:
            raise RuntimeError("fixture or driver did not reply within 10 seconds") from error

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        self.process.stdin.flush()
        return self.receive()

    def tool(self, name, arguments):
        self.sequence += 1
        reply = self.send({"jsonrpc": "2.0", "id": self.sequence, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
        if "error" in reply or reply["result"].get("isError"):
            raise RuntimeError(str(reply))
        return reply["result"]["content"][0]["text"]

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=2)
        self.log_thread.join(timeout=2)


def png_pixel(path, x, y):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG")
    offset, compressed = 8, b""
    while offset < len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        tag, body = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + size]
        if tag == b"IHDR":
            width, height, depth, color, *_ = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color != 2:
                raise AssertionError("unexpected PNG pixel format")
        if tag == b"IDAT":
            compressed += body
        offset += size + 12
    raw = zlib.decompress(compressed)
    stride = width * 3 + 1
    if not 0 <= x < width or not 0 <= y < height or raw[y * stride] != 0:
        raise AssertionError("invalid pixel coordinate or PNG filter")
    start = y * stride + 1 + x * 3
    return tuple(raw[start:start + 3])


def run(output, capture_only=False):
    if output.exists():
        raise ValueError("output already exists; choose a new directory")
    notice = notify_takeover()
    if notice != 0:
        status = "cancelled" if notice == 2 else "notice_failed"
        print(json.dumps({"status": status, "checks": [], "notice_exit_code": notice}))
        return 2 if notice == 2 else 1
    output.mkdir(parents=True)
    dll = win32()
    previous = dll.GetForegroundWindow()
    cursor = wt.POINT()
    cursor_known = dll.GetCursorPos(ctypes.byref(cursor))
    ui = driver = None
    hwnd = None
    checks = []
    report = {"status": "failed", "checks": checks, "scope": "owned_tk_window_only"}
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--fixture"]
        if capture_only:
            command.append("--capture-only")
        ui = Peer(command)
        owned = ui.receive()
        if owned["pid"] != ui.process.pid:
            raise AssertionError("fixture process identity mismatch")
        hwnd = owned["hwnd"]
        driver = Peer([sys.executable, str(Path(__file__).with_name("server.py"))])
        permission = ui.send({"action": "allow", "pid": driver.process.pid})
        report["foreground_grant"] = permission
        report["fixture_handle"] = hwnd
        initialized = driver.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        if initialized.get("result", {}).get("serverInfo", {}).get("name") != "gui-control":
            raise AssertionError("driver did not initialize")
        checks.append("stdio_initialize")
        image = output / "fixture.png"
        driver.tool("screenshot_window", {"hwnd": hwnd, "path": str(image.resolve())})
        state = ui.send({"action": "snapshot"})
        windows = json.loads(driver.tool("list_windows", {}))
        window = next(row for row in windows if row["hwnd"] == hwnd and row["pid"] == ui.process.pid)
        checks.append("owned_window_identity")
        left, top = window["rect"][:2]
        colors = [png_pixel(image, x - left, y - top) for x, y in state["color_points"]]
        if colors != [(255, 0, 0), (0, 255, 0), (0, 0, 255)]:
            raise AssertionError("native capture pixel mismatch: " + str(colors))
        checks.append("screenshot_rgb_positions")
        report.update(fixture_pid=ui.process.pid, screenshot="fixture.png", input_checks="pending")
        if capture_only:
            report.update(status="passed", mode="capture_only", input_checks="not_run")
            return_code = 0
        else:
            return_code = _input_checks(driver, ui, hwnd, checks, report)
            if return_code == 0:
                driver.tool("screenshot_window", {"hwnd": hwnd, "path": str(image.resolve())})
        report["status"] = "passed" if return_code == 0 else "failed"
    except Exception as error:
        report["error"] = str(error)
    finally:
        if driver:
            driver.close()
        if hwnd and dll.GetForegroundWindow() == hwnd and not capture_only:
            if cursor_known:
                dll.SetCursorPos(cursor.x, cursor.y)
            if previous and dll.IsWindow(previous):
                dll.SetForegroundWindow(previous)
        if ui:
            if ui.process.poll() is None:
                try:
                    ui.send({"action": "close"})
                except Exception:
                    pass
            ui.close()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": checks, "report": str(output.resolve() / "report.json")}, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 1


def _wait_snapshot(ui, predicate, timeout=3):
    """Observe queued native events, with a deadline and no input retries."""
    deadline = time.monotonic() + timeout
    state = {}
    while True:
        state = ui.send({"action": "snapshot"})
        if predicate(state):
            return state
        if time.monotonic() >= deadline:
            raise AssertionError("fixture state timed out: " + json.dumps(state, ensure_ascii=True))
        time.sleep(0.05)


def _input_checks(driver, ui, hwnd, checks, report):
    try:
        driver.tool("focus_window", {"hwnd": hwnd})
        expected = "GUI 验收 😀\nsecond line"
        driver.tool("type_text", {"text": expected, "hwnd": hwnd})
        report["after_text"] = _wait_snapshot(ui, lambda state: state["text"] == expected)
        driver.tool("press_key", {"keys": "tab", "mode": "vk", "hwnd": hwnd})
        driver.tool("type_text", {"text": "tab landed", "hwnd": hwnd})
        report["after_entry"] = _wait_snapshot(ui, lambda state: state["entry"] == "tab landed")
        driver.tool("press_key", {"keys": "enter", "mode": "scan", "hwnd": hwnd})
        state = _wait_snapshot(ui, lambda state: state["enters"] == 1)
        if state["text"] != expected or state["entry"] != "tab landed" or state["enters"] != 1:
            raise AssertionError("native input state mismatch: " + json.dumps(state, ensure_ascii=True))
        checks.extend(("unicode_chinese_surrogates_and_newline", "virtual_tab", "scan_enter"))
        driver.tool("click", {"x": state["button"][0], "y": state["button"][1], "hwnd": hwnd})
        state = _wait_snapshot(ui, lambda state: state["clicks"] == 1)
        if state["clicks"] != 1:
            raise AssertionError("native click did not reach fixture button")
        checks.append("targeted_click")
        report.update(status="passed", input_checks="passed",
                      native_state={key: state[key] for key in ("text", "entry", "clicks", "enters")})
        return 0
    except Exception as error:
        report["error"] = str(error)
        report["input_checks"] = "failed"
        return 1


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("This opt-in acceptance check requires an interactive Windows desktop.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, default=Path(".local/desktop-check"))
    parser.add_argument("--capture-only", action="store_true",
                        help="Verify owned-window capture without keyboard or mouse injection")
    args = parser.parse_args()
    if args.fixture:
        fixture()
    else:
        raise SystemExit(run(args.output, args.capture_only))
