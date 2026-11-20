"""Bounded SFTP sessions used only by the shared file pane."""

import socket
import struct
import threading
import time

import paramiko
from paramiko import util
from paramiko.common import DEBUG, byte_ord
from paramiko.sftp import SFTPError

from app import constants


class BrowsingSFTPClient(paramiko.SFTPClient):
    """An SFTP client that bounds each complete packet reply or send."""

    def __init__(self, sock):
        self.timed_out = False
        self._invalid = False
        super().__init__(sock)

    def _invalidate(self):
        if self._invalid:
            return
        self._invalid = True
        self.timed_out = True
        threading.Thread(target=self.sock.close, daemon=True).start()

    def _fail_if_invalid(self):
        if self._invalid:
            raise socket.timeout("file-pane SFTP session is no longer usable")

    def _request(self, t, *args):
        self._fail_if_invalid()
        return super()._request(t, *args)

    def _async_request(self, fileobj, t, *args):
        self._fail_if_invalid()
        return super()._async_request(fileobj, t, *args)

    def _read_all(self, n):
        self._fail_if_invalid()
        deadline = getattr(self, "_packet_deadline", None)
        if deadline is None:
            deadline = time.monotonic() + constants.BROWSE_REPLY_TIMEOUT
        out = bytes()
        try:
            while n > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("file-pane SFTP reply timed out")
                self.sock.settimeout(remaining)
                chunk = self.sock.recv(n)
                if not chunk:
                    raise EOFError()
                out += chunk
                n -= len(chunk)
            return out
        except socket.timeout:
            self._invalidate()
            raise

    def _read_packet(self):
        self._packet_deadline = time.monotonic() + constants.BROWSE_REPLY_TIMEOUT
        try:
            header = self._read_all(4)
            if byte_ord(header[0]):
                raise SFTPError("Garbage packet received")
            size = struct.unpack(">I", header)[0]
            data = self._read_all(size)
        finally:
            self._packet_deadline = None
        if self.ultra_debug:
            self._log(DEBUG, util.format_binary(data, "IN: "))
        if size > 0:
            return byte_ord(data[0]), data[1:]
        return 0, bytes()

    def _write_all(self, out):
        self._fail_if_invalid()
        deadline = time.monotonic() + constants.BROWSE_REPLY_TIMEOUT
        try:
            while out:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("file-pane SFTP request timed out")
                self.sock.settimeout(remaining)
                sent = self.sock.send(out)
                if sent <= 0:
                    raise EOFError()
                out = out[sent:]
        except socket.timeout:
            self._invalidate()
            raise


def close_browsing_sftp(sftp):
    """Close a browsing channel without ever blocking the caller."""
    if sftp is not None:
        threading.Thread(target=sftp.close, daemon=True).start()


def open_browsing_sftp(client):
    """Open one bounded file-pane session without waiting past its deadline."""
    state = {"lock": threading.Lock(), "abandoned": False, "sftp": None, "error": None}
    done = threading.Event()

    def open_session():
        channel = None
        sftp = None
        try:
            transport = client.get_transport() if hasattr(client, "get_transport") else None
            if transport is None:
                sftp = client.open_sftp()
                with state["lock"]:
                    if state["abandoned"]:
                        sftp.close()
                    else:
                        state["sftp"] = sftp
                return
            channel = transport.open_session(timeout=constants.BROWSE_REOPEN_TIMEOUT)
            channel.settimeout(constants.BROWSE_REOPEN_TIMEOUT)
            channel.invoke_subsystem("sftp")
            sftp = BrowsingSFTPClient(channel)
            with state["lock"]:
                if state["abandoned"]:
                    sftp.close()
                else:
                    state["sftp"] = sftp
        except Exception as error:
            if channel is not None and sftp is None:
                try:
                    channel.close()
                except Exception:
                    pass
            with state["lock"]:
                state["error"] = error
        finally:
            done.set()

    threading.Thread(target=open_session, daemon=True).start()
    if not done.wait(constants.BROWSE_REOPEN_TIMEOUT):
        with state["lock"]:
            state["abandoned"] = True
        error = TimeoutError("Could not open a fresh file-pane session in time.")
        error.opener_done = done
        raise error
    with state["lock"]:
        if state["error"] is not None:
            raise RuntimeError("Could not open a fresh file-pane session.") from state["error"]
        return state["sftp"]


def reset_browsing_session(api, action):
    """Replace a timed-out browsing session while its lock is held."""
    sftp = api._sftp
    # A connection already marked lost keeps its timed-out session until the
    # user reconnects; retrying the reopen on every later call would stall
    # each one for the reopen deadline.
    if not getattr(sftp, "timed_out", False) or not api._connected:
        return None
    client = api._client
    generation = api._conn_generation
    api._vlog(f"file-pane request timed out during {action}", "warn", "browse timeout", action=action)
    replacement = None
    try:
        replacement = open_browsing_sftp(client)
    except Exception as error:
        if api._client is client and api._conn_generation == generation:
            api._connected = False
            api._vlog(f"could not reset file-pane session after {action}: {error}", "error",
                      "browse reset failed", action=action)
            return "lost"
        return "stale"
    if api._client is client and api._conn_generation == generation:
        api._sftp = replacement
        api._vlog(f"file-pane session reset after {action}", "ok", "browse reset", action=action)
        return "reset"
    close_browsing_sftp(replacement)
    return "stale"
