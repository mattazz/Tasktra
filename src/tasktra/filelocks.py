"""Local interprocess locks whose ownership ends when the OS closes the handle.

Lock files remain in place: unlinking them would let a waiter and a new caller
lock different inodes. Their contents and age never establish ownership.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import stat
import time
from typing import Iterator


class FileLockError(ValueError):
    """The lock cannot be acquired safely."""


class FileLockBusyError(FileLockError):
    """Another process owns the lock beyond the requested wait."""


def _check_path(path: Path, root: Path) -> None:
    try:
        parts = path.relative_to(root).parts
    except ValueError as error:
        raise FileLockError("lock path escapes the project root") from error
    if not parts or any(part in {".", ".."} for part in parts):
        raise FileLockError("lock path must name a file inside the project root")
    current = root
    for part in parts:
        current /= part
        try:
            details = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            raise FileLockError("lock path contains a symbolic link or reparse point")


def _open_windows(path: Path) -> int:
    # OPEN_REPARSE_POINT opens the link itself, not its target. Denying delete
    # sharing prevents the persistent lock file from being replaced while held.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0xC0000000, 0x3, None, 4, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close(handle)
        raise


def _open_posix(path: Path, root: Path) -> int:
    # Walk directory handles so a swapped parent link cannot redirect creation.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        parts = path.relative_to(root).parts
        for part in parts[:-1]:
            next_directory = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = next_directory
        return os.open(
            parts[-1], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=directory,
        )
    finally:
        os.close(directory)


def _try_acquire(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def exclusive_file_lock(
    path: Path | str, *, root: Path | str, timeout_seconds: float = 0.0,
) -> Iterator[None]:
    """Lock an existing parent directory's regular file, optionally waiting.

The caller must create the parent directory safely. Locks are non-reentrant;
zero timeout preserves optimistic stores' immediate conflict behavior.
"""
    if (not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 60):
        raise FileLockError("lock timeout must be between 0 and 60 seconds")
    anchor = Path(root).resolve(strict=True)
    target = Path(path)
    if not target.is_absolute():
        target = anchor / target
    descriptor = None
    try:
        _check_path(target, anchor)
        descriptor = _open_windows(target) if os.name == "nt" else _open_posix(target, anchor)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise FileLockError("lock must be a regular file with one link")
        _check_path(target, anchor)
        if not os.path.samestat(details, target.stat(follow_symlinks=False)):
            raise FileLockError("lock file changed while being opened")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _try_acquire(descriptor)
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FileLockBusyError("local file is already being updated") from error
                time.sleep(min(0.02, remaining))
        # A Unix caller could unlink a held file. Detect replacement before
        # entering the protected operation; participating callers never unlink.
        _check_path(target, anchor)
        if not os.path.samestat(details, target.stat(follow_symlinks=False)):
            raise FileLockError("lock file changed while waiting")
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        raise FileLockError(f"unable to acquire local file lock: {target}") from error
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        raise
    try:
        yield
    finally:
        # close releases both flock and Windows byte-range locks, including on
        # process termination. No stale-file deletion or clock heuristic needed.
        os.close(descriptor)
