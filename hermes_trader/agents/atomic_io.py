"""Durable, atomic file I/O primitives (single source of truth).

Before this module every state writer reimplemented tmp-file + ``os.replace``
on its own, with subtly different durability guarantees: memory.py and
shadow_book.py fsync'd the file and its parent directory, config_store.py
fsync'd the file but (until callers adopt this module) hand-rolled an EBUSY
fallback, and dsl_exit.py did ``os.replace`` with **no fsync at all** — a
crash between the JSON dump and the rename landing on disk could leave a
zero-length state file. The best-effort cache writers (whale_index,
positions_snapshot, ws_client seen-tids, daemon state) did a bare
tmp + replace with neither fsync nor consistent error handling.

All state/cache writes now go through the helpers here so the durability
contract is expressed exactly once:

  1. write to a tmp file in the SAME directory (a cross-filesystem rename is a
     copy, not atomic);
  2. flush + fsync the file (otherwise os.replace can swap in bytes that have
     not reached disk);
  3. os.replace (atomic on POSIX);
  4. fsync the parent directory (best-effort; makes the rename itself durable
     across power loss — skipped silently on filesystems that refuse it).

``fsync=False`` keeps the atomic-rename (no torn reads) but skips the disk
sync for cheap, regenerable cache files.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

PathLike = str | os.PathLike[str]


def _fsync_dir(path: PathLike) -> None:
    """fsync the directory holding ``path`` so a rename is durable.

    Best-effort: some filesystems (certain CI/sandbox mounts) reject directory
    fsync; the data is already durable in the file itself in that case.
    """
    parent = os.path.dirname(os.fspath(path)) or "."
    try:
        dir_fd = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _replace_with_ebusy_fallback(tmp: str, dst: str, *, ebusy_fallback: bool) -> None:
    """os.replace tmp -> dst, optionally rewriting in place on EBUSY.

    Under a Docker single-file bind mount, ``os.replace`` onto the mounted
    target fails with EBUSY ("Device or resource busy") because the kernel
    cannot swap the inode a mount point points at. When ``ebusy_fallback`` is
    set, fall back to truncating + rewriting the mounted target in place. The
    caller holds an exclusive flock for the whole RMW, so concurrent readers
    see either the old or new contents rather than a torn file.
    """
    try:
        os.replace(tmp, dst)
        return
    except OSError as err:
        if not ebusy_fallback or err.errno != getattr(os, "EBUSY", 16):
            raise
    # EBUSY bind-mount path: copy the tmp bytes over the mounted file.
    with open(tmp, "rb") as src, open(dst, "wb") as out:
        out.write(src.read())
        out.flush()
        os.fsync(out.fileno())
    try:
        os.remove(tmp)
    except OSError:
        pass


def write_json_atomic(
    path: PathLike,
    data: Any,
    *,
    indent: int | None = 2,
    fsync: bool = True,
    ebusy_fallback: bool = False,
    default: Callable[[Any], Any] | None = None,
) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Args:
        path: destination file.
        data: JSON-serializable object.
        indent: json.dump indent (None = compact).
        fsync: fsync file + parent dir for durability. False keeps the atomic
            rename (no torn reads) but skips the disk sync — appropriate for
            cheap, regenerable caches.
        ebusy_fallback: if os.replace fails with EBUSY (Docker single-file
            bind mount), rewrite the target in place instead of raising.
        default: optional json.dump ``default`` serializer for otherwise
            non-JSON-native values (e.g. ``str`` for Path/datetime).

    The parent directory is created if missing.

    Raises:
        OSError: on write failure. The tmp file is always cleaned up and the
            destination is never left truncated.
    """
    path = os.fspath(path)
    parent = os.path.dirname(path) or "."
    # Same-directory tmp keeps the rename atomic (same filesystem) and the
    # unique mkstemp name is collision-free across processes/threads.
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent, default=default)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        _replace_with_ebusy_fallback(tmp, path, ebusy_fallback=ebusy_fallback)
        if fsync:
            _fsync_dir(path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path: PathLike, default: Any = None) -> Any:
    """Read JSON from ``path``; return ``default`` if missing/unreadable."""
    try:
        with open(os.fspath(path), "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        return default


def _lock_path(path: PathLike) -> str:
    return os.fspath(path) + ".lock"


def locked_write_json_atomic(
    path: PathLike,
    data: Any,
    *,
    indent: int | None = 2,
    fsync: bool = True,
    ebusy_fallback: bool = False,
    default: Callable[[Any], Any] | None = None,
) -> None:
    """Like :func:`write_json_atomic` but serialised by a cross-process flock.

    Acquires LOCK_EX on ``<path>.lock`` for the whole write so a trading-loop
    process cannot lose an update racing a dashboard/CLI process writing the
    same file. The lock is released in ``finally`` even on a JSON/IO error.
    """
    lock_fd = os.open(_lock_path(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        write_json_atomic(
            path, data,
            indent=indent, fsync=fsync, ebusy_fallback=ebusy_fallback,
            default=default,
        )
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


@contextmanager
def flock_context(path: PathLike, *, exclusive: bool = True) -> Iterator[None]:
    """Yield while holding a cross-process flock on ``<path>.lock``.

    For read-modify-write callers that already manage their own lock file
    (e.g. config_store.update_agent_config) so they can wrap read + write under
    a single LOCK_EX without adopting locked_write_json_atomic.
    """
    lock_fd = os.open(_lock_path(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
