# -*- coding: utf-8 -*-
"""回归（2026-09 审查 C）：import main_window 不再在顶层拖进 AppKit / openai_server。

实测 AppKit（pyobjc）60ms+，占 import main_window 的 45%；它只在窗口 show
之后的 _setup_macos_window / _install_backtick_monitor 用到，应惰性导入。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_lazy_appkit_import_2026_09_win.py -v
"""
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def _import_chain(module):
    proc = subprocess.run(
        [sys.executable, '-X', 'importtime', '-c', f'import {module}'],
        cwd=ROOT, capture_output=True, text=True, env=dict(os.environ))
    names = set()
    for line in proc.stderr.splitlines():
        if line.startswith('import time:') and '|' in line:
            names.add(line.rsplit('|', 1)[1].strip())
    return names, proc


class TestLazyImports(unittest.TestCase):
    def test_main_window_import_chain_has_no_appkit(self):
        names, proc = _import_chain('main_window')
        self.assertIn('main_window', names, proc.stderr[-2000:])
        self.assertNotIn('AppKit', names)
        self.assertNotIn('openai_server', names)

    def test_macos_native_flag_is_lazy_and_still_exposed(self):
        import main_window
        # 旧名字仍可读（兼容 getattr 用法），但不再是模块导入时就算好的常量
        flag = main_window.MACOS_NATIVE_AVAILABLE
        self.assertIsInstance(flag, bool)
        if sys.platform == 'darwin':
            try:
                import AppKit  # noqa: F401
                self.assertTrue(flag)
            except ImportError:
                self.assertFalse(flag)
        else:
            self.assertFalse(flag)
        # 旧的模块级 OpenAIServerManager 名字也仍然可取
        from openai_server import OpenAIServerManager
        self.assertIs(main_window.OpenAIServerManager, OpenAIServerManager)


class TestShowSmoke(unittest.TestCase):
    """show 之后 _setup_macos_window / backtick 监听器那条延迟路径要能跑通。"""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import main_window
        cls.mw = main_window

    def test_show_runs_deferred_macos_setup_without_error(self):
        from PyQt6 import sip
        from PyQt6.QtCore import QEventLoop, QTimer
        from _qt_quit_reset_2026_09_win import dispose_windows
        w = self.mw.MainWindow()
        try:
            # 监听器是进程级、只装一次：同进程里先跑过的测试可能已经装上，
            # 这里不断言"构造后仍为 None"（单跑本文件时它确实为 None）
            w.show()
            self.app.processEvents()
            # 等过 showEvent 里的 singleShot(100) → _setup_macos_window
            loop = QEventLoop()
            QTimer.singleShot(300, loop.quit)
            loop.exec()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not w._macos_window_configured:
                self.app.processEvents()
            self.assertTrue(w._macos_window_configured)
            self.assertFalse(sip.isdeleted(w))
            if sys.platform == 'darwin' and self.mw.MACOS_NATIVE_AVAILABLE:
                # darwin 且 AppKit 可用：监听器在首次 show 后装上
                self.assertIsNotNone(self.mw.MainWindow._backtick_monitor)
        finally:
            dispose_windows(self.app, [w])


if __name__ == '__main__':
    unittest.main()
