# -*- coding: utf-8 -*-
"""VS Code 面板可用性探测必须在工作线程。

审查发现：面板打开 100ms 后 `_check_vscode` 在 GUI 线程连续 spawn 三次
`code` CLI（--version 两次 + --list-extensions），每次要拉起 Node/Electron，
冷启动 1-3 秒；面板一显示就冻 3-9 秒，最坏 50 秒。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_vscode_probe.py -v
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

import vscode_manager
from vscode_widget import VSCodeExtensionPanel


class TestProbeOffThread(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, cond, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not cond():
            self.app.processEvents()
            time.sleep(0.01)

    def test_check_vscode_spawns_nothing_on_gui_thread(self):
        calls = []

        def fake_run(argv, *a, **k):
            calls.append((threading.get_ident(), list(argv)))
            if '--version' in argv:
                return mock.Mock(returncode=0, stdout='1.96.0\nabc\narm64\n', stderr='')
            return mock.Mock(returncode=0, stdout='ms-python.python\nesbenp.prettier-vscode\n', stderr='')

        with mock.patch.object(vscode_manager.subprocess, 'run', side_effect=fake_run):
            panel = VSCodeExtensionPanel()
            try:
                panel._check_vscode()
                self._pump(lambda: 'VS Code 1.96.0' in panel.vscode_status.text())
                self.assertIn('VS Code 1.96.0', panel.vscode_status.text())
                self.assertTrue(calls, '没有探测到任何 code CLI 调用')
                for tid, argv in calls:
                    self.assertNotEqual(tid, threading.main_thread().ident,
                                        f'{argv} 在 GUI 线程上执行')
                # 一次探测 = 一次 --version + 一次 --list-extensions，不再三次
                versions = [a for _, a in calls if '--version' in a]
                self.assertEqual(len(versions), 1, '--version 被调用了多次')
                self.assertEqual(panel._manager._installed_extensions,
                                 ['ms-python.python', 'esbenp.prettier-vscode'])
            finally:
                panel._manager.shutdown()
                panel.deleteLater()
                for _ in range(3):
                    self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.app.processEvents()

    def test_unavailable_reported_via_slot(self):
        def fake_run(argv, *a, **k):
            raise FileNotFoundError('code')

        with mock.patch.object(vscode_manager.subprocess, 'run', side_effect=fake_run):
            panel = VSCodeExtensionPanel()
            try:
                panel._check_vscode()
                self._pump(lambda: panel.no_vscode_label.isVisibleTo(panel))
                self.assertTrue(panel.no_vscode_label.isVisibleTo(panel))
            finally:
                panel._manager.shutdown()
                panel.deleteLater()
                for _ in range(3):
                    self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
