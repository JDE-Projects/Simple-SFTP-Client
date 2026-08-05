#!/usr/bin/env python3
"""Regenerate screenshots/sftp-client-light-dark.png.

Simple SFTP Client is a desktop app: at screenshot time there is no pywebview
backend to talk to. Its UI is a self-contained HTML file that already
degrades gracefully outside pywebview (every backend call goes through the
`API` object that boot() sets up, and boot only runs on the pywebviewready
event), so this tool serves the page and its assets from a temp folder,
seeds an invented, active SFTP session straight into the page's own `state`
object, and drives the page's own render functions to produce the picture.

Nothing here touches the working copy. The UI file, the icon, and the fonts
folder are copied into a temp folder and served from there; the real files
are only ever read, never written.

    python tools/screenshot/make_screenshot.py

Options:
    --keep            leave the temp folder in place for inspection
    --build-tools P   path to the build-tools repo (default: sibling folder)
    --out P           write the composite image here instead of the default
                       screenshots/sftp-client-light-dark.png (use this to
                       check the output without touching the committed file)
"""

import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

OUT_IMAGE = os.path.join(REPO_ROOT, "screenshots", "sftp-client-light-dark.png")

# Each theme is laid out at this size and captured at half scale, giving two
# 900-wide halves and the 1800x-tall composite the README uses. The height
# is tuned to close just under the queue bar and status bar, with no empty
# band beneath the panes and nothing clipped.
LAYOUT_WIDTH = 1800
LAYOUT_HEIGHT = 620
CAPTURE_SCALE = 0.5


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_app_version() -> str:
    path = os.path.join(REPO_ROOT, "simple_sftp_client.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', source)
    if not match:
        fail(f"could not find APP_VERSION in {path}")
    return match.group(1)


def stage_ui(temp_dir: str) -> None:
    """Copy just what the page needs into temp_dir."""
    shutil.copy2(os.path.join(REPO_ROOT, "simple_sftp_client-UI.html"),
                 os.path.join(temp_dir, "index.html"))
    shutil.copy2(os.path.join(REPO_ROOT, "simple_sftp_client.png"), temp_dir)
    shutil.copytree(os.path.join(REPO_ROOT, "fonts"),
                     os.path.join(temp_dir, "fonts"))


def _index_of(entries, name):
    for i, e in enumerate(entries):
        if e["name"] == name:
            return i
    fail(f"sample entry {name!r} not found in its own fixture list")


def build_setup_script(version: str) -> str:
    """JavaScript that seeds the page's own `state` object with the sample
    session and drives the page's own render path.

    The UI's boot() (named init() here) only runs on the pywebviewready
    event, which never fires in a plain browser, so this calls by hand what
    init() would: it sets the version bar text and the connection fields,
    marks the app connected, seeds state.local / state.remote the way
    list_local()/list_remote() normally would, then calls setCrumbs(),
    renderPane() for both sides, updateXfer() and syncConsoleHeight(), each
    guarded so the script still works if a function is renamed or removed.
    """
    sel_local = _index_of(scene.LOCAL_ENTRIES, scene.SELECTED_LOCAL)
    sel_remote = _index_of(scene.REMOTE_ENTRIES, scene.SELECTED_REMOTE)

    parts = [
        f"document.getElementById('verText').textContent = 'v' + {json.dumps(version)};",
        f"document.getElementById('host').value = {json.dumps(scene.HOST)};",
        f"document.getElementById('port').value = {json.dumps(scene.PORT)};",
        f"document.getElementById('user').value = {json.dumps(scene.USERNAME)};",
        f"document.getElementById('startpath').value = {json.dumps(scene.START_PATH)};",
        "connected = true;",
        "document.getElementById('connBtn').textContent = 'Disconnect';",
        "document.getElementById('connBtn').className = 'btn-danger';",
        "document.getElementById('connDot').className = 'dot live';",
        "document.getElementById('connText').textContent = 'Connected';",
        f"document.getElementById('latency').textContent = {json.dumps(str(scene.LATENCY_MS))} + ' ms';",
        "['compareBtn','syncUpBtn','syncDownBtn','watchBtn','filterRemote'].forEach(function(id){"
        "var el=document.getElementById(id); if(el) el.disabled=false;});",
        f"state.local.entries = {json.dumps(scene.LOCAL_ENTRIES)};",
        f"state.local.cwd = {json.dumps(scene.LOCAL_CWD)};",
        f"state.local.sel = new Set([{sel_local}]);",
        f"state.remote.entries = {json.dumps(scene.REMOTE_ENTRIES)};",
        f"state.remote.cwd = {json.dumps(scene.REMOTE_CWD)};",
        f"state.remote.sel = new Set([{sel_remote}]);",
        "if (typeof setCrumbs === 'function') { setCrumbs('local', state.local.cwd); "
        "setCrumbs('remote', state.remote.cwd); }",
        "if (typeof renderPane === 'function') { renderPane('local'); renderPane('remote'); }",
        "if (typeof updateXfer === 'function') updateXfer();",
        "if (typeof syncConsoleHeight === 'function') syncConsoleHeight();",
        f"({json.dumps(scene.CONSOLE_LINES)}).forEach(function(pair){{ "
        "if (typeof clog === 'function') clog(pair[0], pair[1]); });",
    ]
    return " ".join(parts)


def write_capture_config(temp_dir: str, port: int, version: str) -> str:
    config = {
        "url": f"http://127.0.0.1:{port}/index.html",
        "width": LAYOUT_WIDTH,
        "height": LAYOUT_HEIGHT,
        "scale": CAPTURE_SCALE,
        "outDir": "shots",
        "waitFor": "typeof renderPane === 'function'",
        "setup": build_setup_script(version),
        "settleMs": 500,
        "shots": [
            {"name": "light", "script": "applyTheme('light')"},
            {"name": "dark", "script": "applyTheme('dark')"},
        ],
    }
    path = os.path.join(temp_dir, "shots.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return path


def run(cmd: list, label: str) -> None:
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        fail(f"{label} failed with exit code {result.returncode}")


def main(argv: list) -> None:
    keep = "--keep" in argv
    build_tools = os.path.join(os.path.dirname(REPO_ROOT), "build-tools")
    if "--build-tools" in argv:
        index = argv.index("--build-tools") + 1
        if index >= len(argv):
            fail("--build-tools needs a path after it")
        build_tools = argv[index]

    out_image = OUT_IMAGE
    if "--out" in argv:
        index = argv.index("--out") + 1
        if index >= len(argv):
            fail("--out needs a path after it")
        out_image = argv[index]

    capture_script = os.path.join(build_tools, "screenshot", "capture.mjs")
    compose_script = os.path.join(build_tools, "screenshot", "compose.py")
    for path in (capture_script, compose_script):
        if not os.path.exists(path):
            fail(f"missing {path}. Pass --build-tools with the repo path.")

    version = read_app_version()
    temp_dir = tempfile.mkdtemp(prefix="sftp-screenshot-")
    httpd = None

    try:
        stage_ui(temp_dir)

        port = free_port()

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def __init__(self, *a, **kw):
                super().__init__(*a, directory=temp_dir, **kw)

        httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        config_path = write_capture_config(temp_dir, port, version)
        run(["node", capture_script, config_path], "capture")

        shots_dir = os.path.join(temp_dir, "shots")
        os.makedirs(os.path.dirname(out_image), exist_ok=True)
        run([sys.executable, compose_script, out_image,
             os.path.join(shots_dir, "light.png"),
             os.path.join(shots_dir, "dark.png")], "compose")
    finally:
        if httpd is not None:
            httpd.shutdown()
        if keep:
            print(f"temp folder kept at {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if os.path.exists(temp_dir):
                print(f"WARNING: could not remove {temp_dir}", file=sys.stderr)

    print(f"seeded version: v{version}")
    print(f"updated {out_image}")


if __name__ == "__main__":
    main(sys.argv[1:])
