"""Shared, Qt-independent local file persistence.

Publish only complete, flushed files. Failures propagate to the caller; there
is deliberately no fallback that truncates the existing destination.
"""
import hashlib
import os
import tempfile
from contextlib import contextmanager

from app_logging import get_logger

logger = get_logger(__name__)


@contextmanager
def atomic_writer(path, *, binary=False, preserve_mode=False,
                  follow_symlinks=False, prefix='.stellar_'):
    """Yield a temporary stream, then atomically replace the destination.

    The parent directory must exist. New files are private (mkstemp's 0600).
    Editors opt into following symlinks and retaining existing permission bits;
    config/session writers retain private-file semantics. Text streams use UTF-8.
    Replacement failures, including Windows sharing violations, leave the old
    destination intact. This is not a lock against concurrent writers.
    """
    target = os.path.realpath(path) if follow_symlinks else os.path.abspath(path)
    fd, temporary = tempfile.mkstemp(
        dir=os.path.dirname(target), prefix=prefix, suffix='.tmp')
    try:
        stream = os.fdopen(fd, 'wb' if binary else 'w',
                           **({} if binary else {'encoding': 'utf-8'}))
        fd = None  # ownership transferred to stream
        with stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        if preserve_mode:
            try:
                os.chmod(temporary, os.stat(target).st_mode & 0o777)
            except FileNotFoundError:
                pass  # new destination
        os.replace(temporary, target)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass  # successful replace, or already removed
        except OSError:
            logger.warning('Could not remove temporary file %s', temporary,
                           exc_info=True)


def file_sha256(path):
    """Hash a file without loading another full copy into memory."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()
