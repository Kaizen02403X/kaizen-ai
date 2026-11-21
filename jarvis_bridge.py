from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import shutil
import subprocess
import time
from urllib.parse import urlparse

API_TOKEN = "ad485e20bee833433ded5ff4bd5e6b6d"
HOST = "0.0.0.0"
PORT = 5050

ALLOWED_APPS = {
    "Google Chrome",
    "Safari",
    "Visual Studio Code",
    "Notes",
    "Calculator",
    "Spotify",
    "Finder",
    "Calendar",
    "Preview",
}

BLOCKED_APPLESCRIPT_PATTERNS = re.compile(
    r"\b(?:do\s+shell\s+script|do\s+javascript|delete|remove|rm\s+-rf|keystroke|key\s+code|password|keychain)\b",
    re.IGNORECASE,
)

SWIFT_VISION_OCR = """
import Foundation
import Vision
import AppKit

let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    exit(1)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
try handler.perform([request])
let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
print(lines.joined(separator: "\\n"))
"""

last_call_time = 0.0


def run_osascript(script: str, timeout: int = 10) -> tuple[bool, str]:
    res = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if res.returncode != 0:
        return False, res.stderr.strip() or "AppleScript execution failed."
    return True, res.stdout.strip()


def read_active_tab() -> tuple[int, dict]:
    js = (
        "(function(){"
        "const t=document.body?document.body.innerText:'';"
        "const s=window.getSelection?window.getSelection().toString():'';"
        "return JSON.stringify({title:document.title,url:location.href,selection:s.slice(0,3000),text:t.slice(0,14000)});"
        "})()"
    ).replace('"', '\\"')

    chrome_script = f'''
    if application "Google Chrome" is running then
        tell application "Google Chrome"
            if (count of windows) = 0 then return "NO_WINDOW"
            return execute active tab of front window javascript "{js}"
        end tell
    else
        return "NOT_RUNNING"
    end if
    '''
    ok, out = run_osascript(chrome_script, timeout=10)
    if not ok:
        return 400, {
            "status": "error",
            "message": f"Could not read Chrome tab ({out}). Ensure Chrome -> View -> Developer -> Allow JavaScript from Apple Events is enabled.",
        }
    if out == "NOT_RUNNING":
        return 400, {"status": "error", "message": "Google Chrome is not currently running."}
    if out == "NO_WINDOW":
        return 400, {"status": "error", "message": "Google Chrome has no open windows."}

    try:
        parsed = json.loads(out)
        return 200, {"status": "ok", "browser": "Google Chrome", **parsed}
    except json.JSONDecodeError:
        return 200, {"status": "ok", "browser": "Google Chrome", "text": out[:14000]}


def read_screen() -> tuple[int, dict]:
    img_path = "/tmp/kaizen_screen.png"
    app_script = '''
    tell application "System Events"
        set frontApp to first application process whose frontmost is true
        set appName to name of frontApp
        set winTitle to ""
        try
            set winTitle to name of front window of frontApp
        end try
        return appName & " ||| " & winTitle
    end tell
    '''
    _, app_info = run_osascript(app_script, timeout=5)
    parts = app_info.split(" ||| ", 1)
    front_app = parts[0] if parts else "Unknown"
    window_title = parts[1] if len(parts) > 1 else ""

    cap = subprocess.run(["screencapture", "-x", img_path], capture_output=True, text=True, timeout=8)
    if cap.returncode != 0 or not os.path.exists(img_path):
        return 500, {
            "status": "error",
            "message": "screencapture failed. Enable Screen Recording for Python in System Settings -> Privacy & Security.",
        }

    if not shutil.which("swift"):
        return 500, {
            "status": "error",
            "message": "Apple Command Line Tools (swift) not found. Run 'xcode-select --install' in Terminal.",
        }

    ocr = subprocess.run(
        ["swift", "-", img_path],
        input=SWIFT_VISION_OCR,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if ocr.returncode != 0:
        return 500, {"status": "error", "message": f"Screen OCR failed: {ocr.stderr.strip()}"}

    screen_text = ocr.stdout.strip()[:14000]
    return 200, {
        "status": "ok",
        "front_app": front_app,
        "window_title": window_title,
        "screen_text": screen_text,
    }


def handle_action(payload: dict) -> tuple[int, dict]:
    action = (payload.get("action") or "").strip()

    if action == "open_app":
        app_name = (payload.get("app_name") or "").strip()
        if app_name not in ALLOWED_APPS:
            return 403, {"status": "error", "message": f"App '{app_name}' is not in ALLOWED_APPS."}
        res = subprocess.run(["open", "-a", app_name], capture_output=True, text=True)
        if res.returncode != 0:
            return 400, {"status": "error", "message": res.stderr.strip()}
        return 200, {"status": "ok", "message": f"Opened {app_name}."}

    if action == "open_url":
        url = (payload.get("url") or "").strip()
        browser = (payload.get("browser") or "Google Chrome").strip()
        if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return 400, {"status": "error", "message": "Only http:// and https:// websites are allowed."}
        res = subprocess.run(["open", "-a", browser, url], capture_output=True, text=True)
        if res.returncode != 0:
            return 400, {"status": "error", "message": res.stderr.strip()}
        return 200, {"status": "ok", "message": f"Opened {url} in {browser}."}

    if action == "battery_status":
        res = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True)
        return 200, {"status": "ok", "battery": res.stdout.strip()}

    if action == "run_shortcut":
        name = (payload.get("shortcut_name") or "").strip()
        if not name:
            return 400, {"status": "error", "message": "Missing shortcut_name."}
        res = subprocess.run(["shortcuts", "run", name], capture_output=True, text=True, timeout=20)
        if res.returncode != 0:
            return 400, {"status": "error", "message": res.stderr.strip()}
        return 200, {"status": "ok", "output": res.stdout.strip() or f"Ran shortcut '{name}'."}

    if action == "applescript":
        script = (payload.get("script") or "").strip()
        if not script or BLOCKED_APPLESCRIPT_PATTERNS.search(script):
            return 403, {"status": "error", "message": "AppleScript blocked by safety policy."}
        ok, out = run_osascript(script)
        return (200 if ok else 400), {"status": "ok" if ok else "error", "output": out}

    if action == "read_active_tab":
        return read_active_tab()

    if action == "read_screen":
        return read_screen()

    return 400, {
        "status": "error",
        "message": "Unknown action. Must be one of: open_app, open_url, battery_status, run_shortcut, applescript, read_active_tab, read_screen.",
    }


class BridgeHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, body: dict):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        global last_call_time
        if self.headers.get("X-API-Key", "") != API_TOKEN:
            self._send_json(401, {"status": "error", "message": "Unauthorized"})
            return

        now = time.time()
        if now - last_call_time < 1.0:
            self._send_json(429, {"status": "error", "message": "Rate limited; wait 1 second."})
            return
        last_call_time = now

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, {"status": "error", "message": "Invalid JSON body."})
            return

        code, resp = handle_action(payload)
        self._send_json(code, resp)

    def log_message(self, fmt, *args):
        print(f"[Bridge] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    print(f"JARVIS Host Bridge listening on http://{HOST}:{PORT}")
    HTTPServer((HOST, PORT), BridgeHandler).serve_forever()
