# -*- coding: utf-8 -*-
"""UnixBackend.stop() 不能在 GUI 线程上睡一秒的回归测试。

用户报告：开着多个窗口直接 Quit 有时非常卡。根因：stop() 给 shell 发 SIGTERM
（交互式 shell / 全屏程序普遍忽略它），读取线程退出循环后进 _reap_child(wait=True)
固定轮询 1 秒等一个不会死的子进程，GUI 线程 join 着它——每个终端 ~1.2s，
多窗口多终端串行相加。

    python3 -m pytest tests/test_backend_stop_latency.py -v
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from terminal_backend import IS_WINDOWS, create_backend


@unittest.skipIf(IS_WINDOWS, "Unix pty backend only")
class TestUnixBackendStopLatency(unittest.TestCase):
    def _start(self, cmd):
        backend = create_backend()
        backend.on_output = lambda data: None
        self.assertTrue(backend.start(cmd, cwd=os.path.expanduser('~'),
                                      cols=80, rows=24))
        return backend

    def test_stop_returns_immediately_even_if_child_ignores_sigterm(self):
        # `trap '' TERM` 模拟忽略 SIGTERM 的交互式 shell / TUI 程序
        backend = self._start(['/bin/sh', '-c', "trap '' TERM; while :; do sleep 1; done"])
        pid = backend._child_pid
        time.sleep(0.3)
        t0 = time.monotonic()
        backend.stop()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5, f"stop() blocked for {elapsed:.2f}s")
        self.assertIsNone(backend._reader_thread)
        self.assertIsNone(backend._master_fd)
        self.assertFalse(backend.is_running)
        # 子进程靠 master 关闭后的 SIGHUP 退出，后台线程负责回收
        self._assert_gone(pid)

    def test_stop_reaps_child_that_honours_sigterm(self):
        backend = self._start(['/bin/sh', '-c', 'while :; do sleep 1; done'])
        pid = backend._child_pid
        time.sleep(0.3)
        backend.stop()
        self._assert_gone(pid)

    def _assert_gone(self, pid, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            # 还在：可能是僵尸（已退出未回收）——回收线程会处理；
            # 用 /bin/ps 看状态，Z 也算走了
            try:
                import subprocess
                st = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                    capture_output=True, text=True).stdout.strip()
                if not st or st.startswith('Z'):
                    return
            except Exception:
                pass
            time.sleep(0.1)
        self.fail(f"child {pid} still alive {timeout}s after stop()")


if __name__ == '__main__':
    unittest.main()
