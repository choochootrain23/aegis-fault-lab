"""One collector and workflow owner per local state database."""
import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def controller_ownership(database):
    path = Path(database).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the inode after release: unlinking would let another process lock a
    # replacement file while an existing process still owns the old inode.
    with Path(str(path) + '.controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('This state database already has an active controller. Run one worker per project.') from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
