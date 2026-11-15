"""Bounded, ordered terminal input with cancellable platform I/O.

write() acknowledges queue admission, never waits for a child to read stdin.
Each worker owns its duplicated descriptor/handle until its I/O has stopped.
"""
from collections import deque
import os
import select
import threading
import time

from app_logging import get_logger

logger = get_logger(__name__)


class TerminalWriter:
    MAX_PENDING_BYTES = 16 * 1024 * 1024

    def __init__(self, write_chunk, release, cancel=None):
        self._write_chunk = write_chunk
        self._release = release
        self._cancel = cancel
        self._queue = deque()
        self._pending = 0
        self._active_paste_end = False
        self._condition = threading.Condition()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='terminal-input', daemon=True)
        self.thread.start()

    def write(self, data):
        if not data:
            return not self.stopped.is_set()
        with self._condition:
            if self.stopped.is_set() or len(data) + self._pending > self.MAX_PENDING_BYTES:
                logger.warning('Terminal input rejected: writer closed or input queue full')
                return False
            self._queue.append(bytes(data))
            self._pending += len(data)
            self._condition.notify()
            return True

    def _run(self):
        try:
            while not self.stopped.is_set():
                with self._condition:
                    self._condition.wait_for(lambda: self._queue or self.stopped.is_set())
                    if self.stopped.is_set():
                        break
                    data = self._queue.popleft()
                    self._active_paste_end = (
                        (data.startswith(b'\x1b[200~') and data.endswith(b'\x1b[201~'))
                        or data == b'\x1b[201~\x03')
                view = memoryview(data)
                sent = 0
                while sent < len(view) and not self.stopped.is_set():
                    count = self._write_chunk(view[sent:sent + 4096])
                    if count < 0:
                        raise OSError('Terminal input pipe closed')
                    sent += count
                with self._condition:
                    self._pending -= len(data)
                    self._active_paste_end = False
                    self._condition.notify_all()
        except OSError:
            if not self.stopped.is_set():
                logger.warning('Terminal input failed', exc_info=True)
        finally:
            self.stopped.set()
            with self._condition:
                self._queue.clear()
                self._pending = 0
                self._active_paste_end = False
                self._condition.notify_all()
            self._release()

    def cancel_pending(self):
        """原子停止取队列并记录尚需发送的括号粘贴结束标记。"""
        with self._condition:
            self.stopped.set()
            suffix = b'\x1b[201~' if self._active_paste_end else b''
            self._queue.clear()
            self._condition.notify_all()
            return suffix

    def close(self):
        self.cancel_pending()
        # Bounded cancellation: Unix polls at 50 ms; Windows synchronous pipe
        # writes use CancelSynchronousIo (not CancelIoEx).
        deadline = time.monotonic() + 0.2
        while self.thread.is_alive() and time.monotonic() < deadline:
            if self._cancel:
                self._cancel(self.thread.native_id)
            self.thread.join(0.01)

    def flush(self, timeout=2.0):
        """For verification/shutdown callers; never used by the GUI write path."""
        with self._condition:
            return self._condition.wait_for(lambda: self._pending == 0, timeout)

    @classmethod
    def for_fd(cls, source_fd):
        fd = os.dup(source_fd)
        try:
            os.set_blocking(fd, False)
            def write(chunk):
                try:
                    return os.write(fd, chunk)
                except BlockingIOError:
                    # 先尝试非阻塞写，VINTR 等控制字符可能在普通输入满时仍可被
                    # 行规程接收；真正背压时才等可写，避免忙循环。
                    try:
                        select.select([], [fd], [], 0.05)
                    except InterruptedError:
                        pass
                    return 0
                except InterruptedError:
                    return 0
            return cls(write, lambda: os.close(fd))
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def for_windows_pipe(cls, source_handle):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                          wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
                                          wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.DuplicateHandle.restype = wintypes.BOOL
        kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.WriteFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        process = kernel.GetCurrentProcess()
        handle = wintypes.HANDLE()
        if not kernel.DuplicateHandle(process, source_handle, process,
                                      ctypes.byref(handle), 0, False, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        def write(chunk):
            data = bytes(chunk)
            count = wintypes.DWORD()
            if not kernel.WriteFile(handle, data, len(data), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return count.value if count.value else -1
        def cancel(native_id):
            # https://learn.microsoft.com/windows/win32/fileio/canceling-pending-i-o-operations
            thread = kernel.OpenThread(0x0001, False, native_id)  # THREAD_TERMINATE
            if thread:
                try:
                    kernel.CancelSynchronousIo(thread)
                finally:
                    kernel.CloseHandle(thread)
        try:
            return cls(write, lambda: kernel.CloseHandle(handle), cancel)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
