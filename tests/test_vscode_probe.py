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
                # 只靠面板构造时挂的 100ms 定时器触发探测，别再手动调一次
                # _check_vscode()：两个触发源在 CI 慢机器上会撞出第二次探测
                # （线程已结束、结果尚未投递的窗口里 isRunning() 为 False），
                # 于是 --version 被调两次——macOS job 两次假失败都是它。
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

    def test_probe_async_dedupes_until_result_delivered(self):
        """结果没回到 GUI 线程之前，再多的 probe_async 都不该再起线程。
        模拟"线程已结束、finished 还排在队里"的窗口：把 isRunning 桩成 False。"""
        calls = []

        def fake_run(argv, *a, **k):
            calls.append(list(argv))
            if '--version' in argv:
                return mock.Mock(returncode=0, stdout='1.96.0\n', stderr='')
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch.object(vscode_manager.subprocess, 'run', side_effect=fake_run), \
                mock.patch.object(vscode_manager.ProbeWorker, 'isRunning', return_value=False):
            mgr = vscode_manager.VSCodeManager()
            try:
                mgr.probe_async()
                # 让工作线程真正跑完 run()，但先不处理 GUI 事件（finished 仍在队列里）
                mgr._probe_worker.wait(5000)
                mgr.probe_async()
                mgr.probe_async()
                self._pump(lambda: mgr._probe_pending is False)
                versions = [a for a in calls if '--version' in a]
                self.assertEqual(len(versions), 1, '结果未投递前重复 probe_async 起了第二次探测')
            finally:
                # isRunning 被桩掉后 shutdown() 不会等线程，这里显式等，免得 QThread 析构 abort
                if mgr._probe_worker is not None:
                    mgr._probe_worker.wait(5000)
                mgr.shutdown()

    def test_unavailable_reported_via_slot(self):
        def fake_run(argv, *a, **k):
            raise FileNotFoundError('code')

        with mock.patch.object(vscode_manager.subprocess, 'run', side_effect=fake_run):
            panel = VSCodeExtensionPanel()
            try:
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
