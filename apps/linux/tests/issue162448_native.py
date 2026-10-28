#!/usr/bin/env python3
"""Fork-only actual Tauri/X11 proof; full UI, synthetic Gateway, no native mocks."""

import argparse
import html
import json
import mimetypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import unquote, urlsplit

from issue162448_native_gateway import NativeGatewayFixture, QuickChatHandler
from window_chrome import WindowChromeFixture


MARKER = "assistant-panel-caption-clearance"


class Handler(QuickChatHandler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return super().do_GET()
        if path.endswith("/control-ui-config.json"):
            self.reply(200, self.server.bootstrap, "application/json")
            return
        for name in ("mock-gateway.js", "observer.js"):
            if path == "/fixture/" + name:
                self.reply(200, (self.server.fixture_root / name).read_bytes(), "text/javascript")
                return
        if path == "/fixture/":
            self.send_response(302)
            self.send_header("Location", self.server.workpath)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        assets_index = path.find("/assets/")
        relative = path[assets_index + 1:] if assets_index >= 0 else (path.removeprefix("/fixture/") if path.startswith("/fixture/") else path.lstrip("/"))
        asset = (self.server.ui_root / relative).resolve()
        if asset.is_relative_to(self.server.ui_root) and asset.is_file():
            self.reply(200, asset.read_bytes(), mimetypes.guess_type(str(asset))[0] or "application/octet-stream")
            return
        if assets_index >= 0 or Path(relative).suffix:
            self.reply(404)
            return
        if path.startswith("/fixture/"):
            document = (self.server.ui_root / "index.html").read_text()
            document = document.replace("<html", '<html data-openclaw-control-ui-base-path="/fixture" data-openclaw-bootstrap-config="' + html.escape(self.server.bootstrap.decode(), quote=True) + '"', 1)
            document = document.replace("<head>", '<head><script src="/fixture/mock-gateway.js"></script><script defer src="/fixture/observer.js"></script>', 1)
            self.reply(200, document.encode(), "text/html; charset=utf-8")
            return
        self.reply(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/fixture/proof" or not 0 < length < 131072:
            self.reply(400)
            return
        self.server.observation = json.loads(self.rfile.read(length))
        self.server.observation_count += 1
        self.reply(200, b"{}", "application/json")


def run_worker(args):
    server = NativeGatewayFixture(args.artifacts_dir)
    server.RequestHandlerClass = Handler
    server.ui_root = args.ui_root
    server.fixture_root = args.fixture_root
    server.bootstrap = (args.fixture_root / "bootstrap.json").read_bytes()
    server.workpath = (args.fixture_root / "workpath.txt").read_text().strip()
    server.observation = {}
    server.observation_count = 0
    chrome = WindowChromeFixture(args.artifacts_dir)
    app = None
    server_started = False
    result = {"phase": args.phase, "status": "failed", "marker": MARKER, "checks": [],
              "rendering": {"backend": os.environ.get("GDK_BACKEND"),
                            "WEBKIT_DISABLE_COMPOSITING_MODE": os.environ.get("WEBKIT_DISABLE_COMPOSITING_MODE")}}

    def record(name, value=True):
        result["checks"].append({"name": name, "value": value})
        print(name + ": " + json.dumps(value), flush=True)

    def wait(predicate, description, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.failure:
                raise RuntimeError("Synthetic native Gateway: " + server.failure)
            if app is not None and app.poll() is not None:
                raise RuntimeError("Native app exited while waiting for " + description)
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise RuntimeError("Timed out waiting for " + description)

    def find_window():
        for line in chrome.command("wmctrl", "-lp").splitlines():
            fields = line.split(None, 4)
            if len(fields) == 5 and fields[2] == str(app.pid) and "OpenClaw" in fields[4]:
                return fields[0]
        return None

    def observation():
        return server.observation

    def control(name):
        value = observation().get("controls", {}).get(name, {})
        return value if value.get("visible") and not value.get("disabled") else None

    def click(name):
        button = wait(lambda: control(name), "visible " + name)
        window = wait(find_window, "native window")
        window_box = chrome.geometry(window)
        box = button["rect"]
        record("pointer-click", {"control": name, "label": button["label"],
                                "snapshotSequence": observation().get("sequence"), "rect": box})
        chrome.command("xdotool", "mousemove", "--sync", str(round(window_box["x"] + box["x"] + box["width"] / 2)),
                       str(round(window_box["y"] + box["y"] + box["height"] / 2)), "click", "1")

    def panel_visible():
        return observation().get("panel", {}).get("visible", False)

    def open_panel():
        click("homeFooter")
        wait(panel_visible, "Home assistant panel")
        # Opening Home and collapsing navigation have separate DOM commits.
        # Match the canonical browser regression: await the Expand control itself.
        wait(lambda: control("expandSidebar") and control("expandSidebar")["clickableAtCenter"],
             "hittable Expand sidebar after Home opens")
        click("expandSidebar")
        wait(lambda: "shell--nav-collapsed" not in observation().get("shellClasses", "") and control("panelClose"), "expanded regular navigation")
        assert "shell--mobile-nav" not in observation().get("shellClasses", ""), "Expected regular desktop navigation"
        assert "assistant-panel--right" in observation()["panel"]["className"], "Expected right-docked assistant"

    def maximize(expected):
        click("maximize")
        wait(lambda: ("_NET_WM_STATE_MAXIMIZED_HORZ" in chrome.state(wait(find_window, "native window"))) == expected,
             "real X11 maximize/restore state")
        expected_label = "Restore window" if expected else "Maximize window"
        wait(lambda: control("maximize") and control("maximize")["label"] == expected_label, "caption state readback")
        record("native-maximized", expected)

    def overlap():
        snap = observation()
        caption = snap["caption"]["rect"]
        collisions = []
        for button in snap["headerButtons"]:
            if not button.get("visible"):
                continue
            box = button["rect"]
            if (box["x"] < caption["x"] + caption["width"] and box["x"] + box["width"] > caption["x"]
                    and box["y"] < caption["y"] + caption["height"] and box["y"] + box["height"] > caption["y"]):
                collisions.append(button["label"])
        return collisions

    def screenshot(name):
        # Same capture owner as the repository's native window-chrome scenario.
        chrome.capture(name)
        source = args.artifacts_dir / ("window-chrome-" + name + ".png")
        destination = args.artifacts_dir / ("assistant-native-" + name + ".png")
        source.rename(destination)
        return destination.name

    try:
        chrome.start()
        server.start()
        server_started = True
        with Path("app.log").open("wb") as log:
            app = subprocess.Popen([str(args.binary)], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        wait(lambda: control("homeFooter") and control("maximize"), "full Control UI and real native caption", timeout=75)
        window_bounds = chrome.geometry(wait(find_window, "native window"))
        assert observation()["viewport"]["devicePixelRatio"] == 1, "Expected X11 CSS-to-device scale one"
        assert observation()["viewport"]["width"] == window_bounds["width"], "Native WebView must fill client width"
        record("full-ui-native-caption-loaded", {"window": wait(find_window, "native window"), "path": observation().get("pathname")})
        open_panel()
        maximize(True)
        wait(lambda: observation().get("caption", {}).get("visible") and len(observation().get("headerButtons", [])) >= 4, "assistant header geometry")
        time.sleep(0.4)
        result["initialGeometry"] = observation()
        result["screenshot"] = screenshot(args.phase)
        collisions = overlap()
        record("caption-overlap", collisions)
        if args.phase == "before":
            if not collisions:
                raise RuntimeError("Original stylesheet did not reproduce caption overlap")
            click("panelClose")
            wait(lambda: not find_window(), "covered panel-close click to hide native window")
            record("covered-panel-close-hid-native-window", True)
            result["status"] = "expected-regression"
            print(MARKER + ": original CSS has native overlap and wrong close target", flush=True)
            return 1
        if collisions:
            raise RuntimeError(MARKER + ": " + repr(collisions))
        for expected in (False, True):
            maximize(expected)
            assert not overlap(), MARKER
            click("ask")
            wait(lambda: observation().get("activePanelMode") == "custodian", "Ask OpenClaw selected")
            click("panelClose")
            wait(lambda: not panel_visible(), "assistant close")
            assert find_window(), "Panel close hid the native window"
            record("panel-close-preserves-native-window", True)
            open_panel()
            assert not overlap(), MARKER
        # Exercise the narrow supported header with actual X11 pointer movement.
        divider = wait(lambda: control("divider"), "assistant divider")["rect"]
        bounds = chrome.geometry(find_window())
        chrome.drag(round(bounds["x"] + divider["x"] + divider["width"] / 2),
                    round(bounds["y"] + divider["y"] + divider["height"] / 2), 200, 0)
        wait(lambda: abs(observation()["panel"]["rect"]["width"] - 320) < 1, "minimum assistant width")
        assert not overlap(), MARKER
        header = observation()["header"]
        for tab in header["tabs"]:
            if tab["visible"]:
                assert tab["rect"]["x"] + tab["rect"]["width"] <= header["actionsRect"]["x"], "Narrow tabs overlap panel actions"
        record("minimum-width", observation()["panel"]["rect"]["width"])
        screenshot("after-narrow")
        click("dockBottom")
        wait(lambda: "assistant-panel--bottom" in observation()["panel"]["className"], "bottom docking")
        assert not overlap(), MARKER
        click("dockRight")
        wait(lambda: "assistant-panel--right" in observation()["panel"]["className"], "right docking")
        assert not overlap(), MARKER
        click("panelClose")
        wait(lambda: not panel_visible(), "final panel close")
        assert find_window(), "Final panel close hid the native window"
        window = find_window()
        click("minimize")
        wait(lambda: "Iconic" in chrome.state(window), "native minimize")
        chrome.command("wmctrl", "-ia", window)
        wait(lambda: "Iconic" not in chrome.state(window), "restore minimized native window")
        record("native-minimize", True)
        click("windowClose")
        wait(lambda: not find_window(), "native close to tray")
        record("native-close-to-tray", True)
        result["status"] = "passed"
        return 0
    except BaseException as error:
        result["error"] = str(error)
        result["lastObservation"] = observation()
        traceback.print_exc()
        try:
            # Only this task-private session; no argv, environment or credentials.
            result["processState"] = chrome.command(
                "ps", "--sid", str(os.getsid(0)), "-o", "pid,ppid,stat,pcpu,wchan:40,comm")
        except BaseException as diagnostic_error:
            result["processStateError"] = type(diagnostic_error).__name__
        try:
            screenshot(args.phase + "-failed")
        except BaseException:
            pass
        return 2
    finally:
        result["observationCount"] = server.observation_count
        result["nativeRequests"] = server.native_requests
        (args.artifacts_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        if app is not None and app.poll() is None:
            app.terminate()
            try:
                app.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.kill()
                app.wait(timeout=5)
        if server_started:
            server.shutdown()
        server.server_close()
        chrome.passed = result["status"] == "passed"
        chrome.checks = result["checks"]
        chrome.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--ui-root", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--artifacts-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() == 0:
        parser.error("Use non-root Linux with the WebKit sandbox enabled")
    for key in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        if not os.environ.get(key):
            parser.error("Run inside xvfb-run and private dbus-run-session")
    for name in ("binary", "ui_root", "fixture_root"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    args.artifacts_dir = args.artifacts_dir.resolve()
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)
    if args.worker:
        return run_worker(args)
    with tempfile.TemporaryDirectory(prefix="openclaw-assistant-native-") as directory:
        root = Path(directory)
        env = {key: os.environ[key] for key in ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY") if key in os.environ}
        env.update(HOME=str(root), PATH="/usr/bin:/bin", LANG="C.UTF-8", LC_ALL="C.UTF-8", GDK_BACKEND="x11",
                   XDG_SESSION_TYPE="x11", GTK_MODULES="atk-bridge", NO_AT_BRIDGE="0", PYTHONDONTWRITEBYTECODE="1")
        # A rendering-only diagnostic after default Xvfb painting/input stalled.
        # Official WebKit HardwareAccelerationManager maps this to compositing;
        # the WebKit process sandbox and all production native bridges stay intact.
        # Both original and fixed UI phases use this identical mode.
        env["WEBKIT_DISABLE_COMPOSITING_MODE"] = "1"
        for key, relative in (("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache"), ("XDG_DATA_HOME", ".local/share"),
                              ("XDG_STATE_HOME", ".local/state"), ("XDG_RUNTIME_DIR", "runtime"), ("TMPDIR", "tmp")):
            path = root / relative
            path.mkdir(mode=0o700, parents=True)
            env[key] = str(path)
        command = [sys.executable, str(Path(__file__).resolve()), str(args.binary), "--worker", "--phase", args.phase,
                   "--ui-root", str(args.ui_root), "--fixture-root", str(args.fixture_root), "--artifacts-dir", str(args.artifacts_dir)]
        worker = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            return worker.wait(timeout=240)
        finally:
            try:
                os.killpg(worker.pid, signal.SIGTERM)
                worker.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.wait(timeout=5)
            log = root / "app.log"
            if log.is_file():
                (args.artifacts_dir / "app.log").write_bytes(log.read_bytes())


if __name__ == "__main__":
    raise SystemExit(main())
