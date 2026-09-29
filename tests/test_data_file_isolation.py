"""Verify the shared fixture keeps ordinary app persistence out of the repo."""
import json
from pathlib import Path

import simple_sftp_client as app
from app import paths, prefs


def test_app_save_paths_use_the_isolated_data_folder(isolate_app_data_files):
    """The normal save paths write only to the per-test data folder."""
    data_dir = isolate_app_data_files
    repo_root = Path(__file__).resolve().parents[1]

    assert app.save_prefs({"theme": "light"}) is True
    result = app.Api(app.APP_VERSION).save_session({
        "name": "isolated",
        "host": "example.com",
        "port": "22",
        "username": "alice",
        "auth": "password",
        "key_path": "",
        "start_path": "",
        "remember": False,
    })

    assert result["ok"] is True
    assert Path(prefs._pref_path()).parent == data_dir
    assert Path(paths.SESSIONS_FILE).parent == data_dir
    assert (data_dir / "simple_sftp_client.pref").is_file()
    assert json.loads((data_dir / "servers.json").read_text(encoding="utf-8"))["sessions"]
    assert Path(prefs._pref_path()) != repo_root / "simple_sftp_client.pref"
    assert Path(paths.SESSIONS_FILE) != repo_root / "servers.json"
