#!/usr/bin/env python3
"""The private key-file route: an empty 0600 file in a private 0700 folder for the user to paste a key into.

    python3 mezmo_key_file.py create-key-file          # prints the new file's path, nothing else
    python3 mezmo_key_file.py delete-key-file <path>

mezmo_setup.py --key-file <path> --delete-key-file reads the key from it and deletes it, so the key
never goes through a chat, a command line or the governed Bash arguments.

The functions below are copied byte for byte from the Claude Code plugin's scripts/mezmo_common.py,
because the Codex plugin ships only this folder. test_setup.py checks they still match.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import stat
import sys
import tempfile
from typing import Optional, Sequence, Tuple


def _require_posix() -> None:
    # O_NOFOLLOW, O_DIRECTORY, dir_fd and getuid are POSIX-only; Windows is not supported.
    if os.name != "posix":
        raise ValueError("Private key files need macOS or Linux; this platform is not supported.")


def _private_key_fd(path: pathlib.Path) -> Tuple[int, int]:
    """Open without following the file or parent symlink; reject shared files before reading."""
    _require_posix()
    directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError("Key file must be in a private directory owned by this user (mode 0700).")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("Key file must be a regular, unshared file owned by this user (mode 0600).")
        return directory, fd
    except Exception:
        if fd is not None:
            os.close(fd)
        os.close(directory)
        raise


def read_key_file(path: pathlib.Path) -> str:
    try:
        directory, fd = _private_key_fd(path)
        try:
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("Key file is too large.")
            key = raw.decode("utf-8").strip()
            if not key or any(c in key for c in "\r\n\x00"):
                raise ValueError("Key file must contain one nonempty token line.")
            return key
        finally:
            os.close(directory)
    except (OSError, UnicodeError):
        raise ValueError("Could not read a private UTF-8 key file; check its ownership, permissions and path.") from None


def delete_key_file(path: pathlib.Path) -> None:
    directory, fd = _private_key_fd(path)
    try:
        # Check that the path still names the opened file before unlinking.
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        opened = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Key file changed before cleanup; remove it manually.")
        os.unlink(path.name, dir_fd=directory)
    finally:
        os.close(fd)
        os.close(directory)
    if path.parent.name.startswith("mezmo-key-"):
        try:
            path.parent.rmdir()  # Only an empty helper-created directory.
        except OSError:
            pass


def key_file_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or remove a private temporary key file; accepts no secret arguments.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-key-file")
    remove = sub.add_parser("delete-key-file")
    remove.add_argument("path", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        _require_posix()
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    try:
        if args.command == "create-key-file":
            folder = pathlib.Path(tempfile.mkdtemp(prefix="mezmo-key-"))
            os.chmod(folder, 0o700)
            path = folder / "key"
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            print(path)
        else:
            delete_key_file(args.path)
        return 0
    except (OSError, ValueError):
        print("Could not prepare or remove the private key file. Check permissions; never print its contents.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(key_file_main())
