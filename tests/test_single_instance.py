"""Verify that only one client launcher process can run at a time."""

import ctypes
import os
import subprocess
import sys
import uuid

import pytest

import simple_sftp_client as launcher


def _unique_mutex_name():
    # Never the real app's name, so a running copy of the app cannot affect
    # the tests and the tests cannot block the app.
    return f"JDE_SimpleSFTPClient_Test_{uuid.uuid4().hex}"


@pytest.fixture
def calls(monkeypatch):
    record = {"focus": [], "messages": []}
    monkeypatch.setattr(launcher, "_focus_existing_window",
                        lambda title: record["focus"].append(title) or record["focus_result"])
    monkeypatch.setattr(launcher, "_show_message",
                        lambda title, text, icon: record["messages"].append((text, icon)))
    record["focus_result"] = True
    return record


def test_first_copy_continues_without_focus_or_message(monkeypatch, calls):
    monkeypatch.setattr(launcher, "_acquire_single_instance", lambda name: True)

    launcher.ensure_single_instance()

    assert calls["focus"] == []
    assert calls["messages"] == []


def test_second_copy_focuses_running_window_and_exits(monkeypatch, calls):
    monkeypatch.setattr(launcher, "_acquire_single_instance", lambda name: False)

    with pytest.raises(SystemExit) as exit_info:
        launcher.ensure_single_instance()

    assert exit_info.value.code == 0
    assert calls["focus"] == [launcher.APP_TITLE]
    assert calls["messages"] == []


def test_second_copy_says_already_running_when_window_not_found(monkeypatch, calls):
    monkeypatch.setattr(launcher, "_acquire_single_instance", lambda name: False)
    calls["focus_result"] = False

    with pytest.raises(SystemExit) as exit_info:
        launcher.ensure_single_instance()

    assert exit_info.value.code == 0
    assert len(calls["messages"]) == 1
    text, icon = calls["messages"][0]
    assert "already running" in text
    assert icon == launcher.MB_ICONINFORMATION


def test_lock_failure_shows_error_and_does_not_start(monkeypatch, calls):
    def fail(name):
        raise OSError("lock unavailable")
    monkeypatch.setattr(launcher, "_acquire_single_instance", fail)

    with pytest.raises(SystemExit) as exit_info:
        launcher.ensure_single_instance()

    assert exit_info.value.code == 1
    assert calls["focus"] == []
    text, icon = calls["messages"][0]
    assert "will not start" in text
    assert icon == launcher.MB_ICONERROR


def test_message_box_failure_still_exits(monkeypatch):
    def fail(name):
        raise OSError("lock unavailable")
    monkeypatch.setattr(launcher, "_acquire_single_instance", fail)

    class BrokenUser32:
        def MessageBoxW(self, *args):
            raise OSError("no message box")

    class BrokenWindll:
        user32 = BrokenUser32()

    monkeypatch.setattr(launcher.ctypes, "windll", BrokenWindll())

    with pytest.raises(SystemExit) as exit_info:
        launcher.ensure_single_instance()
    assert exit_info.value.code == 1


def test_lock_creation_failure_raises(monkeypatch):
    class FailingCreate:
        restype = None

        def __call__(self, *args):
            ctypes.set_last_error(5)

    class FakeKernel32:
        CreateMutexW = FailingCreate()

    monkeypatch.setattr(launcher.ctypes, "WinDLL", lambda *a, **k: FakeKernel32())

    with pytest.raises(OSError):
        launcher._acquire_single_instance(_unique_mutex_name())


@pytest.mark.parametrize("class_name, expected", [
    ("Qt6112QWindowIcon", True),
    ("Qt6120QWindowIcon", True),
    ("CabinetWClass", False),
    ("Chrome_WidgetWin_1", False),
    ("", False),
])
def test_only_qt_app_windows_count_as_the_app(class_name, expected):
    assert launcher._is_app_window_class(class_name) is expected


_HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
import simple_sftp_client as launcher
print("first" if launcher._acquire_single_instance(sys.argv[2]) else "second", flush=True)
sys.stdin.readline()
"""


def test_lock_is_held_by_a_running_copy_and_released_when_it_exits():
    name = _unique_mutex_name()
    repo = os.path.dirname(os.path.abspath(launcher.__file__))
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, repo, name],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "first"

        second = subprocess.run(
            [sys.executable, "-c", _HOLDER, repo, name],
            input="\n", capture_output=True, text=True, timeout=60, check=True)
        assert second.stdout.strip() == "second"
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=60)

    again = subprocess.run(
        [sys.executable, "-c", _HOLDER, repo, name],
        input="\n", capture_output=True, text=True, timeout=60, check=True)
    assert again.stdout.strip() == "first"
