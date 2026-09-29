"""Protect the public pywebview bridge while the app implementation moves."""
import inspect
import json
import re
import sys
from importlib import import_module, metadata
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


MANIFEST_PATH = Path(__file__).with_name("bridge_manifest.json")
HTML_PATH = Path(__file__).resolve().parents[1] / "simple_sftp_client-UI.html"


def build_manifest():
    """Return the API names and parameters pywebview exposes from a fresh Api."""
    api_module = import_module("app.api")
    launcher = import_module("simple_sftp_client")
    exposed_objects = []

    # Copied from pywebview 6.2.1 webview/util.py inject_pywebview's nested
    # get_args and get_functions rules. Recheck these rules before changing
    # the pinned pywebview version.
    def get_args(func):
        return list(inspect.getfullargspec(func).args)

    def get_functions(obj, base_name="", functions=None):
        obj_id = id(obj)
        if obj_id in exposed_objects:
            return functions
        exposed_objects.append(obj_id)

        if functions is None:
            functions = {}

        for name in dir(obj):
            try:
                full_name = f"{base_name}.{name}" if base_name else name
                if name.startswith("_"):
                    continue

                attr = getattr(obj, name)
                if not getattr(attr, "_serializable", True):
                    continue

                if inspect.ismethod(attr) or inspect.isfunction(attr):
                    functions[full_name] = get_args(attr)[1:]
                elif inspect.isclass(attr) or (
                    isinstance(attr, object)
                    and not callable(attr)
                    and hasattr(attr, "__module__")
                ):
                    get_functions(attr, full_name, functions)
            except Exception:  # noqa: BLE001, S112
                continue

        return functions

    return dict(sorted(get_functions(api_module.Api(launcher.APP_VERSION)).items()))


def _manifest_difference(expected, actual):
    """Describe manifest names added, removed, or changed by a bridge edit."""
    expected_names = set(expected)
    actual_names = set(actual)
    added = sorted(actual_names - expected_names)
    removed = sorted(expected_names - actual_names)
    changed = sorted(
        name for name in expected_names & actual_names if expected[name] != actual[name]
    )
    return added, removed, changed


def _write_manifest():
    """Write the current manifest for an intentional approved bridge change."""
    MANIFEST_PATH.write_text(
        json.dumps(build_manifest(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_manifest_matches_checked_in_contract():
    expected = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    actual = build_manifest()
    added, removed, changed = _manifest_difference(expected, actual)
    assert actual == expected, (
        "Bridge manifest changed. "
        f"Added: {added}; removed: {removed}; changed: {changed}"
    )


def test_pywebview_version_matches_copied_discovery_rules():
    version = metadata.version("pywebview")
    assert version == "6.2.1", (
        f"Installed pywebview is {version}. The discovery rules copied into this test "
        "must be rechecked against the new version before updating the pin here."
    )


def test_html_bridge_calls_are_exposed():
    html = HTML_PATH.read_text(encoding="utf-8")
    api_calls = set(re.findall(r"\bAPI\.([A-Za-z_]\w*)", html))
    direct_calls = set(re.findall(
        r"\bwindow\.pywebview\.api\.([A-Za-z_]\w*)", html
    ))
    called_names = api_calls | direct_calls

    assert called_names
    assert {"connect", "save_theme"} <= called_names
    missing = sorted(called_names - set(build_manifest()))
    assert not missing, f"HTML bridge calls missing from manifest: {missing}"


if __name__ == "__main__":
    # Rewrite only for an intentional, approved bridge change.
    _write_manifest()
