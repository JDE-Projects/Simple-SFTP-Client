"""Drive the real page against the real backend in headless Edge.

Serves simple_sftp_client-UI.html to a hidden Edge window. Every bridge call
the page makes is passed to a real simple_sftp_client.Api, connected to a
throwaway in-process SFTP server (tools/sftp_server_core.py). scenario.js
then clicks through the page's own functions and reports each check back.

    .venv/Scripts/python.exe tools/ui_check/run_ui_check.py
    .venv/Scripts/python.exe tools/ui_check/run_ui_check.py --page OTHER.html

--page runs the same checks against another copy of the page, for example
one saved from main with `git show main:simple_sftp_client-UI.html`, to
confirm a check fails without the change it guards.

What it does not cover: the real pywebview window and bridge (plain HTTP
stands in for it), the real connect path (the Api is connected the way the
pytest fixtures do it, so no host key or saved session is read or written),
and the update check (answered locally, no network call).

Everything is written to a temp folder under %TEMP% and removed on exit.
Exit code 0 only when every check passes.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, REPO)
import paramiko  # noqa: E402

import simple_sftp_client  # noqa: E402
from tools import sftp_server_core  # noqa: E402
from tools.sftp_server_core import PASSWORD, USER  # noqa: E402

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
TIMEOUT_S = 180


def long_path(path):
    """Edge's profile nests past the 260-character limit; the \\\\?\\ form
    lets the cleanup reach it."""
    return "\\\\?\\" + os.path.abspath(path) if os.name == "nt" else path


def make_fixture(work):
    server_root = os.path.join(work, "server")
    local = os.path.join(work, "local")
    other_local = os.path.join(work, "other_local")
    for d in (server_root, os.path.join(server_root, "other"), local,
              os.path.join(local, "sub"), other_local):
        os.makedirs(d)
    files = [(os.path.join(local, n), n) for n in ("a.txt", "b.txt", "c.txt")] + [
        (os.path.join(local, "sub", "insub.txt"), "x"),
        (os.path.join(other_local, "up.txt"), "up"),
        (os.path.join(server_root, "0dl.txt"), "d0"),
        (os.path.join(server_root, "dl1.txt"), "d1"),
        (os.path.join(server_root, "other", "o.txt"), "o"),
    ]
    for path, data in files:
        with open(path, "w") as f:
            f.write(data)
    return server_root, {"local": local, "sub": os.path.join(local, "sub"), "other": other_local}


def build_page(page_path, targets):
    html = open(page_path, encoding="utf-8").read()
    scenario = open(os.path.join(HERE, "scenario.js"), encoding="utf-8").read()
    shim = ("<script>\n"
            "window.pywebview={api:new Proxy({},{get:(t,name)=>name===\"then\"?undefined:\n"
            "  (...args)=>fetch('/api/'+name,{method:'POST',body:JSON.stringify(args)}).then(r=>r.json())})};\n"
            f"window.__T={json.dumps(targets)};\n"
            "setTimeout(()=>window.dispatchEvent(new Event('pywebviewready')),0);\n"
            "</script>")
    idx = html.rindex("</body>")
    return html[:idx] + shim + "<script>" + scenario + "</script>" + html[idx:]


def main(argv):
    page_path = os.path.join(REPO, "simple_sftp_client-UI.html")
    if "--page" in argv:
        page_path = os.path.abspath(argv[argv.index("--page") + 1])
    edge_exe = next((p for p in EDGE_PATHS if os.path.exists(p)), None)
    if not edge_exe:
        print("Microsoft Edge not found; cannot run the UI check.")
        return 2

    work = tempfile.mkdtemp(prefix="sftp-ui-check-")
    server_root, targets = make_fixture(work)
    fs_cls = sftp_server_core.make_fs(server_root)
    srv_sock, port = sftp_server_core.start(fs_cls, paramiko.RSAKey.generate(2048))

    api = simple_sftp_client.Api()
    simple_sftp_client.debug.on_warning = api._on_debug_warning
    # Point the debug log at a folder that doesn't exist, so toggling it on
    # always fails: this exercises the "write failed, warn, turn off" path
    # deterministically, and keeps a real debug log from ever landing in the
    # repo (the real log_dir is exe_dir(), the repo root, when run from source).
    simple_sftp_client.debug.log_dir = os.path.join(work, "no_such_debug_folder")
    api._watch_interval = 0.5
    delay_once, calls, results = {}, {}, {}
    done = threading.Event()

    def fake_connect(_params):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=port, username=USER, password=PASSWORD,
                       look_for_keys=False, allow_agent=False)
        api.client, api.sftp, api.connected = client, client.open_sftp(), True
        return {"ok": True, "cwd": "/"}

    version = simple_sftp_client.APP_VERSION
    overrides = {
        "connect": fake_connect,
        "get_meta": lambda: {"version": version, "sessions": [], "key_types": ["Ed25519"]},
        "get_theme": lambda: "dark",
        "check_update": lambda: {"current": version, "version": None, "update": False, "offline": False},
    }
    html = build_page(page_path, targets)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, obj, ctype="application/json"):
            body = obj if isinstance(obj, str) else json.dumps(obj)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(html, "text/html; charset=utf-8")
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
            if self.path.startswith("/api/"):
                name = self.path[5:]
                calls[name] = calls.get(name, 0) + 1
                ms = delay_once.pop(name, 0)
                if ms:
                    time.sleep(ms / 1000)
                fn = overrides.get(name) or getattr(api, name)
                try:
                    return self._send(fn(*body))
                except Exception as e:
                    return self._send({"ok": False, "error": f"ui_check: {e!r}"})
            if self.path == "/fs":
                op, path = body["op"], body.get("path", "")
                if path and not os.path.abspath(path).startswith(work):
                    return self._send({"ok": False, "error": "outside the check's temp folder"})
                if op == "write":
                    with open(path, "w") as f:
                        f.write(body["data"])
                    return self._send({"ok": True})
                if op == "exists":
                    return self._send({"ok": os.path.exists(os.path.join(server_root, body["rel"]))})
                if op == "delay":
                    delay_once[body["method"]] = body["ms"]
                    return self._send({"ok": True})
                if op == "calls":
                    return self._send(dict(calls))
            if self.path == "/report":
                results.update(body)
                done.set()
                return self._send({"ok": True})
            self._send({"ok": False})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    edge = subprocess.Popen(
        [edge_exe, "--headless=new", f"--user-data-dir={os.path.join(work, 'edge')}",
         "--no-first-run", "--no-default-browser-check", "--disable-extensions", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        finished = done.wait(TIMEOUT_S)
    finally:
        subprocess.run(["taskkill", "/PID", str(edge.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            api.shutdown()
        except Exception:
            pass
        httpd.shutdown()
        srv_sock.close()

    checks = results.get("checks", [])
    for c in checks:
        print(("PASS " if c["pass"] else "FAIL ") + c["name"]
              + ("" if c["pass"] else f"  [{c['detail']}]"))
    if not finished:
        print(f"TIMED OUT: no report from the page within {TIMEOUT_S} s")
    if results.get("error"):
        print("SCENARIO ERROR:", results["error"])

    for _ in range(20):
        shutil.rmtree(long_path(work), ignore_errors=True)
        if not os.path.exists(work):
            break
        time.sleep(0.5)
    if os.path.exists(work):
        print("Temp folder NOT removed:", work)

    passed = finished and not results.get("error") and checks and all(c["pass"] for c in checks)
    print(f"{sum(c['pass'] for c in checks)}/{len(checks)} checks passed")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
