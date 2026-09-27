"""Filesystem-backed SFTP server shared by the tests and the manual test server.

tests/conftest.py starts it in-process on a free port for the pytest fixtures;
tools/test_sftp_server.py runs it on 127.0.0.1:2222 for manual smoke tests.
Both serve a local folder as "/" and accept one password login.

Behavior switches are class attributes on FS. Subclass it (or build one with
make_fs) to change them:
    ROOT                    the local folder served as "/"
    REPORT_TIMES            False leaves file times out of stat and listing
                            replies, like a server that omits them
    POSIX_RENAME_SUPPORTED  False answers posix-rename as unsupported, like a
                            server without the posix-rename@openssh.com
                            extension
    SET_TIME_SUPPORTED      False refuses to set file times

Design notes worth keeping:
  - canonicalize() returns a POSIX-clean absolute path. paramiko's default uses
    os.path, which on Windows hands the client back-slashed or doubled paths
    and leaves the app's remote pane blank.
  - posix_rename() must be supported for uploads to work at all: the app
    publishes a finished upload with it and refuses the upload when a server
    lacks it, to protect the existing copy.
"""
import os
import posixpath
import socket
import threading

import paramiko

USER = "test"
PASSWORD = "testpass"


def _attrs(st, report_times=True):
    """Build SFTP attributes from an os.stat result. With report_times False,
    both times are left out, so the reply carries no modification time at all,
    the way a server that omits file times would answer."""
    attr = paramiko.SFTPAttributes.from_stat(st)
    if not report_times:
        attr.st_atime = None
        attr.st_mtime = None
    return attr


class Handle(paramiko.SFTPHandle):
    REPORT_TIMES = True

    def stat(self):
        try:
            return _attrs(os.fstat(self.readfile.fileno()), self.REPORT_TIMES)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)


class FS(paramiko.SFTPServerInterface):
    ROOT = None
    REPORT_TIMES = True
    POSIX_RENAME_SUPPORTED = True
    SET_TIME_SUPPORTED = True

    def _real(self, path):
        p = path if posixpath.isabs(path) else "/" + path
        p = posixpath.normpath(p).strip("/")
        return os.path.join(self.ROOT, *p.split("/")) if p else self.ROOT

    def list_folder(self, path):
        rp = self._real(path)
        try:
            out = []
            for name in os.listdir(rp):
                attr = _attrs(os.stat(os.path.join(rp, name)), self.REPORT_TIMES)
                attr.filename = name
                out.append(attr)
            return out
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return _attrs(os.stat(self._real(path)), self.REPORT_TIMES)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def lstat(self, path):
        try:
            return _attrs(os.lstat(self._real(path)), self.REPORT_TIMES)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def open(self, path, flags, attr):
        rp = self._real(path)
        try:
            flags |= getattr(os, "O_BINARY", 0)
            fd = os.open(rp, flags, 0o666)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        if flags & os.O_WRONLY:
            mode = "ab" if flags & os.O_APPEND else "wb"
        elif flags & os.O_RDWR:
            mode = "a+b" if flags & os.O_APPEND else "r+b"
        else:
            mode = "rb"
        try:
            f = os.fdopen(fd, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        h = Handle(flags)
        h.REPORT_TIMES = self.REPORT_TIMES
        h.filename = rp
        h.readfile = f
        h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._real(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._real(oldpath), self._real(newpath))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def posix_rename(self, oldpath, newpath):
        # os.replace overwrites the target atomically, matching real
        # posix-rename servers. The base SFTPServerInterface implementation
        # returns unsupported, which is what a server without the extension
        # does; POSIX_RENAME_SUPPORTED False models that.
        if not self.POSIX_RENAME_SUPPORTED:
            return paramiko.SFTP_OP_UNSUPPORTED
        try:
            os.replace(self._real(oldpath), self._real(newpath))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def chattr(self, path, attr):
        if not self.SET_TIME_SUPPORTED:
            return paramiko.SFTP_OP_UNSUPPORTED
        try:
            if getattr(attr, "st_mtime", None) is not None:
                atime = attr.st_atime if getattr(attr, "st_atime", None) is not None else attr.st_mtime
                os.utime(self._real(path), (atime, attr.st_mtime))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def canonicalize(self, path):
        if not path.startswith("/"):
            path = "/" + path
        return posixpath.normpath(path)


class Server(paramiko.ServerInterface):
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_auth_password(self, username, password):
        if username == USER and password == PASSWORD:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"


def make_fs(root, **switches):
    """Return an FS subclass serving root, with any of the class-attribute
    switches above overridden."""
    attrs = {"ROOT": str(root)}
    attrs.update(switches)
    return type("FSServing", (FS,), attrs)


def _serve(sock, host_key, fs_cls):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        t = paramiko.Transport(conn)
        t.add_server_key(host_key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, fs_cls)
        try:
            t.start_server(server=Server())
        except Exception:
            continue


def start(fs_cls, host_key, host="127.0.0.1", port=0):
    """Listen on host:port (0 picks a free port) and serve fs_cls on a daemon
    thread. Returns (listening socket, bound port). Closing the socket stops
    new connections; the caller owns closing it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(16)
    except OSError:
        sock.close()
        raise
    threading.Thread(target=_serve, args=(sock, host_key, fs_cls), daemon=True).start()
    return sock, sock.getsockname()[1]
