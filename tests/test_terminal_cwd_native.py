# -*- coding: utf-8 -*-
"""不 fork 地取进程 cwd（_cwd_via_native）的测试。

背景：get_cwd() 原本只有 `lsof -a -d cwd -p <pid>` 一条路径——每次约 27ms，
且在 GUI 线程同步调用（切标签页 / 分屏 / 粘贴图片都会触发）。cwd 落在挂死的
网络挂载点上时 lsof 会一路顶到 2s 超时，多标签场景下就是「别的终端也跟着卡」。
原生接口（Linux 读 /proc，macOS 走 libproc）约 0.2ms 且不会阻塞。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_terminal_cwd_native.py -v
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from terminal_widget import _cwd_via_native

_SUPPORTED = sys.platform == 'darwin' or sys.platform.startswith('linux')


@unittest.skipUnless(_SUPPORTED, "仅 macOS / Linux 有原生取 cwd 的实现")
class TestCwdViaNative(unittest.TestCase):
    def test_reads_child_process_cwd(self):
        target = os.path.realpath(tempfile.mkdtemp(prefix='cwd_native_'))
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                             cwd=target)
        try:
            # 给子进程一点时间完成 exec
            deadline = time.time() + 3
            got = None
            while time.time() < deadline:
                got = _cwd_via_native(p.pid)
                if got:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(got, "应能读到子进程 cwd")
            self.assertEqual(os.path.realpath(got), target)
        finally:
            p.kill()
            p.wait()

    def test_dead_pid_returns_none_without_raising(self):
        # 不存在的 pid 必须安静返回 None（调用方会回退到 lsof）
        self.assertIsNone(_cwd_via_native(999999))

    def test_is_fast_enough_for_gui_thread(self):
        """热路径必须远快于 lsof（~27ms），否则失去替换意义。"""
        _cwd_via_native(os.getpid())          # 预热（懒加载 libproc）
        t0 = time.perf_counter()
        for _ in range(20):
            _cwd_via_native(os.getpid())
        avg_ms = (time.perf_counter() - t0) * 1000 / 20
        self.assertLess(avg_ms, 5.0,
                        f"单次耗时 {avg_ms:.2f}ms，未达到「远快于 lsof」的目标")

    def test_own_process_matches_getcwd(self):
        got = _cwd_via_native(os.getpid())
        self.assertIsNotNone(got)
        self.assertEqual(os.path.realpath(got), os.path.realpath(os.getcwd()))


if __name__ == '__main__':
    unittest.main()
