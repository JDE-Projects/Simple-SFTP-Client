"""Service functions for the keys area."""

import os
import io
import time
import tempfile
import posixpath
import webview
import paramiko
from app.debug import debug
from app.errors import friendly_error
from app.keyfiles import _protect_private_key


def default_key_path(api, key_type):
    name = "id_ed25519" if (key_type or "").startswith("Ed25519") else "id_rsa"
    return os.path.join(os.path.expanduser("~"), ".ssh", name)


def browse_save_key(api, suggested):
    if not api._window:
        return ""
    try:
        dlg = webview.FileDialog.SAVE
    except AttributeError:  # older pywebview
        dlg = webview.SAVE_DIALOG
    res = api._window.create_file_dialog(
        dlg, save_filename=suggested or "id_ed25519")
    if not res:
        return ""
    return res if isinstance(res, str) else res[0]


def generate_key(api, key_type, out_path, passphrase, overwrite=False):
    out_path = (out_path or "").strip().strip('"')
    if not out_path:
        return {"ok": False, "error": "Enter a save location for the key."}
    default_name = "id_ed25519" if key_type.startswith("Ed25519") else "id_rsa"
    # if a folder (or trailing slash) was given, drop the default key name in it
    if os.path.isdir(out_path) or out_path.endswith(("\\", "/")):
        out_path = os.path.join(out_path, default_name)
    created_dir = None
    parent = os.path.dirname(out_path) or "."
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
            created_dir = parent
        except OSError:
            return {"ok": False, "error": f"Couldn't create the folder {parent} \u2014 choose a location you can write to."}

    pub_path = out_path + ".pub"
    existing = [p for p in (out_path, pub_path) if os.path.exists(p)]
    if existing and not overwrite:
        return {"ok": False, "needs_overwrite": True, "private_path": out_path,
                "public_path": pub_path, "existing": existing}

    tmp_priv = tmp_pub = None
    backup_path = None
    try:
        if key_type.startswith("Ed25519"):
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives import serialization
            k = Ed25519PrivateKey.generate()
            enc = (serialization.BestAvailableEncryption(passphrase.encode())
                   if passphrase else serialization.NoEncryption())
            priv = k.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.OpenSSH, enc)
            pub = k.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                               serialization.PublicFormat.OpenSSH)
        else:
            key = paramiko.RSAKey.generate(4096)
            buf = io.StringIO()
            key.write_private_key(buf, password=passphrase or None)
            priv = buf.getvalue().encode()
            pub = f"ssh-rsa {key.get_base64()}".encode()
        pubtext = pub.decode().strip() + " simple-sftp-client"
        pub_bytes = (pubtext + "\n").encode("utf-8")

        # Write both files to temp files in the same folder first, so the
        # final publish below is an atomic rename and a crash mid-write
        # can never leave a truncated key on disk.
        fd, tmp_priv = tempfile.mkstemp(dir=parent, prefix=".sftpkey_priv_")
        with os.fdopen(fd, "wb") as f:
            f.write(priv)
            f.flush()
            os.fsync(f.fileno())
        if os.path.getsize(tmp_priv) != len(priv):
            raise OSError("The private key file didn't write completely.")

        protection_warning = _protect_private_key(tmp_priv)

        fd, tmp_pub = tempfile.mkstemp(dir=parent, prefix=".sftpkey_pub_")
        with os.fdopen(fd, "wb") as f:
            f.write(pub_bytes)
            f.flush()
            os.fsync(f.fileno())
        if os.path.getsize(tmp_pub) != len(pub_bytes):
            raise OSError("The public key file didn't write completely.")

        # Back up the existing private key to a durable file on disk (not
        # just in memory) before touching anything. The private key is
        # published first, so the only half-replaced state to undo is a
        # successful private swap followed by a failed public swap:
        # restoring this backup returns the old, working pair. A failed
        # private swap changes nothing, and the public is never swapped
        # before it. Keeping the backup on disk (rather than in a
        # variable) means that even a hard exit between the two swaps
        # leaves a recoverable copy of the old private key behind.
        if os.path.exists(out_path):
            with open(out_path, "rb") as f:
                old_priv = f.read()
            bfd, backup_path = tempfile.mkstemp(dir=parent, prefix=".sftpkey_bak_")
            with os.fdopen(bfd, "wb") as f:
                f.write(old_priv)
                f.flush()
                os.fsync(f.fileno())
            if os.path.getsize(backup_path) != len(old_priv):
                raise OSError("The private key backup didn't write completely.")
            # This is a backup of the old key, not the user's active key,
            # so its own protection warning (if any) isn't surfaced.
            _protect_private_key(backup_path)

        os.replace(tmp_priv, out_path)
        tmp_priv = None
        try:
            os.replace(tmp_pub, pub_path)
            tmp_pub = None
        except Exception:
            # Public key publish failed after the private key was already
            # replaced: put the old private key back so the pair stays
            # consistent, then report the failure. The restore is an
            # atomic rename (never an open(..., "wb") rewrite), so a
            # failure partway through the restore can't truncate the
            # only remaining copy of the private key.
            if backup_path is not None:
                # Windows can refuse the rename with "Access is denied"
                # for a few milliseconds while antivirus or the search
                # indexer holds the just-published key open, so retry
                # briefly before giving up.
                restored = False
                for attempt in range(5):
                    try:
                        os.replace(backup_path, out_path)
                        restored = True
                        break
                    except OSError:
                        if attempt < 4:
                            time.sleep(0.05)
                kept_backup, backup_path = backup_path, None
                if not restored:
                    # Leave the durable backup on disk as the recovery
                    # artifact instead of letting `finally` delete it;
                    # out_path still holds a complete (if mismatched)
                    # new key, never a truncated one. Say so, so the
                    # user isn't left with a pair that quietly no
                    # longer matches.
                    debug.log("KEYGEN restore failed",
                              {"path": out_path, "backup": kept_backup})
                    return {"ok": False, "error": (
                        "The key couldn't be saved, and the old private key "
                        f"couldn't be put back. {out_path} now holds a new "
                        "private key that doesn't match the old public key. "
                        f"The old private key is saved as {kept_backup}. "
                        f"Rename it to {os.path.basename(out_path)} to keep "
                        "using the old key.")}
            else:
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            raise

        debug.log("KEYGEN", {"type": key_type, "path": out_path})
        if backup_path is not None:
            try:
                os.remove(backup_path)
                backup_path = None
            except OSError:
                pass
        result = {"ok": True, "public": pubtext, "private_path": out_path,
                  "public_path": pub_path, "created_dir": created_dir}
        if protection_warning:
            result["protection_warning"] = protection_warning
        return result
    except PermissionError:
        return {"ok": False, "error": "Couldn't write there (permission denied). Choose a folder you can write to, such as your user's .ssh folder."}
    except OSError as e:
        return {"ok": False, "error": f"Couldn't save the key: {e.strerror or str(e) or 'write failed'}. Try a different location."}
    except Exception:
        return {"ok": False, "error": "Key generation failed. Check the type and passphrase and try again."}
    finally:
        # A leftover backup_path here means a pre-swap exception hit
        # before either os.replace ran, so the old files are untouched
        # and the backup is unneeded. Success and the rollback both
        # already consumed the backup and reset it to None, so this
        # never removes a backup a hard exit still needs for manual
        # recovery (finally doesn't run on a hard exit anyway).
        for t in (tmp_priv, tmp_pub, backup_path):
            if t and os.path.exists(t):
                try:
                    os.remove(t)
                except OSError:
                    pass


def install_pubkey(api, pubtext):
    if not api.connected:
        return {"ok": False, "error": "Not connected."}
    try:
        home = api.sftp.normalize(".")
        ssh_dir = posixpath.join(home, ".ssh")
        try:
            api.sftp.stat(ssh_dir)
        except Exception:
            api.sftp.mkdir(ssh_dir)
            api.sftp.chmod(ssh_dir, 0o700)
        ak = posixpath.join(ssh_dir, "authorized_keys")
        existing = ""
        try:
            with api.sftp.open(ak, "r") as f:
                existing = f.read().decode()
        except Exception:
            pass
        if pubtext.split()[1] in existing:
            return {"ok": True, "already": True}
        with api.sftp.open(ak, "a") as f:
            f.write(("" if existing.endswith("\n") or not existing else "\n") + pubtext + "\n")
        api.sftp.chmod(ak, 0o600)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": friendly_error(e)}
