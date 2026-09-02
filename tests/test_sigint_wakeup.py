# -*- coding: utf-8 -*-
"""Ctrl+C 两步退出：不再靠 120ms 常驻定时器轮询唤醒解释器。

审查发现：install_sigint_handler 用一个 120ms 的常驻 QTimer 让 Python 信号
处理器有机会执行，空闲时也每秒 8 次唤醒。修复：signal.set_wakeup_fd 挂到
socketpair 上，QSocketNotifier 收到字节才处理；"4 秒内再按一次"用单发定时器。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_sigint_wakeup.py -v
"""
import os
import signal
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


@unittest.skipIf(sys.platform == 'win32', 'os.kill(SIGINT) semantics differ on Windows')
class TestSigintWakeup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._old_handler = signal.getsignal(signal.SIGINT)
        self._old_wakeup = signal.set_wakeup_fd(-1)
        signal.set_wakeup_fd(self._old_wakeup)

    def tearDown(self):
        # 拆掉本次安装的通知器/套接字，恢复 pytest 自己的处理器
        cleanup = getattr(self.app, '_sigint_cleanup', None)
        if cleanup is not None:
            cleanup()
        signal.set_wakeup_fd(self._old_wakeup)
        signal.signal(signal.SIGINT, self._old_handler)

    def _pump(self, seconds, until=lambda: False):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not until():
            self.app.processEvents()
            time.sleep(0.005)

    def test_ctrl_c_is_handled_without_polling_timer(self):
        import app as app_mod
        notes = []
        app_mod.install_sigint_handler(self.app, notify=notes.append)

        timer = getattr(self.app, '_sigint_timer', None)
        self.assertTrue(timer is None or not timer.isActive(),
                        "仍在用常驻定时器轮询信号")

        state = self.app._sigint_state
        self.assertFalse(state['armed'])
        os.kill(os.getpid(), signal.SIGINT)
        self._pump(1.0, until=lambda: state['armed'])
        self.assertTrue(state['armed'], "SIGINT 没有经 wakeup fd 唤醒事件循环")
        self.assertTrue(notes and 'Ctrl+C' in notes[-1], "第一次 Ctrl+C 应打印提示")

    def test_disarm_after_window_uses_single_shot_timer(self):
        import app as app_mod
        app_mod.install_sigint_handler(self.app, notify=lambda _t: None,
                                       rearm_window=0.2)
        state = self.app._sigint_state
        os.kill(os.getpid(), signal.SIGINT)
        self._pump(1.0, until=lambda: state['armed'])
        self.assertTrue(state['armed'])
        self._pump(0.6, until=lambda: not state['armed'])
        self.assertFalse(state['armed'], "超过时间窗后应自动复位")


if __name__ == '__main__':
    unittest.main()
