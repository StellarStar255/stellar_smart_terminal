"""Cancellable POSIX subprocess pipes with a progress-based idle timeout.

ControlMaster streams large files without communicate() buffering them in RAM.
All pipe waits, including stderr draining and process exit, share one deadline;
only transferred payload bytes extend it. No helper threads outlive a transfer.
"""
import os
import selectors
import subprocess
import time

from app_logging import get_logger
from i18n import t

logger = get_logger(__name__)


class TransferCancelledError(RuntimeError):
    def __init__(self):
        super().__init__(t('remote.transfer_cancelled'))


class TransferTimeoutError(TimeoutError):
    def __init__(self, seconds):
        super().__init__(t('remote.transfer_timeout', seconds=seconds))


class ProcessPipeIO:
    MAX_STDERR_BYTES = 64 * 1024

    def __init__(self, proc, *, timeout, cancelled=lambda: False):
        if timeout <= 0:
            raise ValueError('Transfer timeout must be positive')
        self.proc = proc
        self.timeout = timeout
        self.cancelled = cancelled
        self._deadline = time.monotonic() + timeout
        self._stdin = self._stdout = self._stderr = None
        self._errors = bytearray()
        self._selector = selectors.DefaultSelector()

    def __enter__(self):
        try:
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(self.proc, name, None)
                if stream is not None:
                    fd = stream.fileno()
                    os.set_blocking(fd, False)
                    setattr(self, '_' + name, fd)
            self._check()
            return self
        except BaseException:
            self._cleanup()
            raise

    def _check(self):
        if self.cancelled():
            raise TransferCancelledError()
        if time.monotonic() >= self._deadline:
            raise TransferTimeoutError(self.timeout)

    def _progress(self):
        self._deadline = time.monotonic() + self.timeout

    def _ready(self, reads, writes, timeout):
        desired = {fd: selectors.EVENT_READ for fd in reads}
        for fd in writes:
            desired[fd] = desired.get(fd, 0) | selectors.EVENT_WRITE
        for fd in list(self._selector.get_map()):
            if fd not in desired:
                self._selector.unregister(fd)
        for fd, events in desired.items():
            key = self._selector.get_map().get(fd)
            if key is None:
                self._selector.register(fd, events)
            elif key.events != events:
                self._selector.modify(fd, events)
        events = self._selector.select(timeout)
        return ([key.fd for key, mask in events if mask & selectors.EVENT_READ],
                [key.fd for key, mask in events if mask & selectors.EVENT_WRITE])

    def _wait(self, mode):
        """Drain stderr while waiting for the requested pipe or child exit."""
        while True:
            self._check()
            if mode == 'finish' and self.proc.poll() is not None and self._stderr is None:
                return
            reads = [self._stderr] if self._stderr is not None else []
            if mode == 'read' and self._stdout is not None:
                reads.append(self._stdout)
            writes = [self._stdin] if mode == 'write' and self._stdin is not None else []
            remaining = self._deadline - time.monotonic()
            try:
                ready, writable = self._ready(reads, writes, max(0, min(.1, remaining)))
            except InterruptedError:
                continue
            self._check()
            if self._stderr is not None and self._stderr in ready:
                try:
                    chunk = os.read(self._stderr, self.MAX_STDERR_BYTES)
                except (BlockingIOError, InterruptedError):
                    chunk = None
                if chunk == b'':
                    self._stderr = None
                elif chunk:
                    self._errors.extend(chunk)
                    if len(self._errors) > self.MAX_STDERR_BYTES:
                        del self._errors[:-self.MAX_STDERR_BYTES]
            if mode == 'write' and self._stdin in writable:
                return
            if mode == 'read' and self._stdout in ready:
                return
            if mode == 'finish':
                if self.proc.poll() is not None and self._stderr is None:
                    return
            elif mode == 'write' and self.proc.poll() is not None:
                raise BrokenPipeError('Remote transfer process exited before input completed')

    def read(self, size):
        if size <= 0:
            raise ValueError('Read size must be positive')
        while self._stdout is not None:
            self._wait('read')
            try:
                chunk = os.read(self._stdout, size)
            except (BlockingIOError, InterruptedError):
                continue
            if chunk:
                self._progress()
            else:
                self._stdout = None
            return chunk
        return b''

    def write(self, data):
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            if self._stdin is None:
                raise BrokenPipeError('Remote transfer input is closed')
            self._wait('write')
            try:
                count = os.write(self._stdin, view[sent:])
            except (BlockingIOError, InterruptedError):
                continue
            if count <= 0:
                raise BrokenPipeError('Remote transfer input closed during write')
            sent += count
            self._progress()
        return sent

    def close_input(self):
        if self._stdin is not None:
            self.proc.stdin.close()  # os.write bypasses buffering, so close cannot flush pending bytes.
            self._stdin = None

    def finish(self):
        self.close_input()
        self._wait('finish')
        self.proc.wait(timeout=max(.001, self._deadline - time.monotonic()))
        return bytes(self._errors).decode('utf-8', 'replace')

    def _cleanup(self):
        if self.proc.poll() is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                logger.debug('Transfer process already exited')
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.warning('Transfer process did not exit after kill')
        finally:
            self._selector.close()
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(self.proc, name, None)
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        logger.debug('Transfer pipe already closed', exc_info=True)

    def __exit__(self, exc_type, exc, tb):
        self._cleanup()
