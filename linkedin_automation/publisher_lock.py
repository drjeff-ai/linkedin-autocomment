"""One publisher per channel, across processes (D-022 F6).

Two things can now write one channel's queue to Buffer: the dashboard (its
Schedule button and the post drain, through ``dashboard._gated_schedule_pass``)
and the headless ``topup`` command a Windows task runs. The dashboard's own
busy check (``can_start_scheduling_task``) sees only jobs inside the dashboard
process, so it cannot see a ``topup`` run, and ``topup`` cannot see it.

Two passes over one state file at once could each read a row as unscheduled
before either wrote, and create two Buffer posts for it. Nothing can unpublish
the second. So both take this lock, keyed by the channel's state file, and the
second one refuses instead of waiting.

The lock is an OS byte-range lock on ``<state file>.lock`` (``msvcrt`` on
Windows, ``fcntl`` elsewhere). The OS drops it when the holder's process dies,
so a crashed run never leaves the channel locked. It is per open handle, so two
holders in one process also exclude each other. The file's text names the
current holder, for the refusal message only; the lock itself is the OS lock,
never the text.
"""

import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

#: The byte locked. Past the holder text, so a refused caller can still read
#: who holds it (Windows will not read a locked range).
_LOCK_OFFSET = 1 << 20


class PublisherBusy(RuntimeError):
    """Another publisher holds this channel. Nothing was sent."""


def lock_path(state_path: str) -> str:
    """The lock file for the channel whose state file is ``state_path``."""
    return state_path + ".lock"


def _try_lock(f) -> bool:
    if sys.platform == "win32":
        import msvcrt
        f.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(f) -> None:
    if sys.platform == "win32":
        import msvcrt
        f.seek(_LOCK_OFFSET)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _holder_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(500).strip("\x00 \r\n") or "unknown"
    except OSError:
        return "unknown"


@contextmanager
def channel_lock(state_path: str, holder: str):
    """Hold the publisher lock for one channel, or raise :class:`PublisherBusy`.

    Never blocks: a second publisher refuses at once and says who holds it.
    """
    path = lock_path(state_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    try:
        if not _try_lock(f):
            raise PublisherBusy(
                "another publisher is writing this channel to Buffer (%s); "
                "nothing was sent. Try again when it finishes."
                % _holder_text(path))
        try:
            f.seek(0)
            f.truncate()
            f.write("%s, pid %d, since %s\n" % (
                holder, os.getpid(),
                datetime.now(timezone.utc).isoformat(timespec="seconds")))
            f.flush()
            yield
        finally:
            _unlock(f)
    finally:
        f.close()
