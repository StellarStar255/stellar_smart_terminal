# -*- coding: utf-8 -*-
"""explorer 粘贴 / 拖放前的目录大小统计与覆盖 rmtree 不能在 GUI 线程跑。

审查发现：_handle_drop_copy / _clipboard_paste_into 把复制本身交给了工作
线程，但复制前的 local_entry_size（os.walk 最多 5 万条目）和"覆盖"时的
shutil.rmtree(dst) 仍在 GUI 线程同步执行——大目录期间窗口整个冻住。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_paste_offthread_2026_09_edit.py -v
"""
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import explorer_clipboard  # noqa: E402
import explorer_common  # noqa: E402
import explorer_widget  # noqa: E402
from explorer_widget import ExplorerPanel  # noqa: E402


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='explorer_paste_'))
        self.panel = ExplorerPanel()
        self.panel.set_root_path(self.tmp)
        self.app.processEvents()
        self.src = os.path.join(self.tmp, 'srcdir')
        os.makedirs(self.src)
        with open(os.path.join(self.src, 'f.txt'), 'w') as f:
            f.write('data')
        self.target = os.path.join(self.tmp, 'target')
        os.makedirs(self.target)
        self.gui_ident = threading.get_ident()

    def tearDown(self):
        explorer_clipboard.clear()
        self.panel.deleteLater()
        for _ in range(3):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spy(self, module, name):
        """记录 module.name 每次被调用时所在线程，返回 (patcher, 记录列表)。"""
        seen = []
        orig = getattr(module, name)

        def wrapper(*a, **k):
            seen.append(threading.get_ident())
            return orig(*a, **k)
        return mock.patch.object(module, name, side_effect=wrapper), seen

    def _assert_off_gui(self, seen, what):
        self.assertTrue(seen, f"{what} 没有被调用")
        self.assertNotIn(self.gui_ident, seen, f"{what} 在 GUI 线程里跑了")


class TestDropCopy(_Base):
    def test_size_scan_off_gui_thread(self):
        patcher, seen = self._spy(explorer_widget, 'local_entry_size')
        with patcher:
            self.panel._handle_drop_copy([self.src], self.target)
        self._assert_off_gui(seen, 'local_entry_size')
        self.assertTrue(os.path.isfile(os.path.join(self.target, 'srcdir', 'f.txt')))

    def test_overwrite_rmtree_off_gui_thread(self):
        dst = os.path.join(self.target, 'srcdir')
        os.makedirs(dst)
        with open(os.path.join(dst, 'old.txt'), 'w') as f:
            f.write('old')
        patcher, seen = self._spy(shutil, 'rmtree')
        with patcher, mock.patch.object(
                QMessageBox, 'question',
                return_value=QMessageBox.StandardButton.Yes):
            self.panel._handle_drop_copy([self.src], self.target)
        self._assert_off_gui(seen, 'shutil.rmtree')
        self.assertFalse(os.path.exists(os.path.join(dst, 'old.txt')))
        self.assertTrue(os.path.isfile(os.path.join(dst, 'f.txt')))


class TestClipboardPaste(_Base):
    def test_size_scan_off_gui_thread(self):
        explorer_clipboard.set_items([("local", self.src)])
        patcher, seen = self._spy(explorer_widget, 'local_entry_size')
        with patcher:
            self.panel._clipboard_paste_into(self.target)
        self._assert_off_gui(seen, 'local_entry_size')
        self.assertTrue(os.path.isfile(os.path.join(self.target, 'srcdir', 'f.txt')))

    def test_overwrite_rmtree_off_gui_thread(self):
        dst = os.path.join(self.target, 'srcdir')
        os.makedirs(dst)
        with open(os.path.join(dst, 'old.txt'), 'w') as f:
            f.write('old')
        explorer_clipboard.set_items([("local", self.src)])
        patcher, seen = self._spy(shutil, 'rmtree')
        with patcher, mock.patch.object(
                explorer_common, 'resolve_paste_conflict',
                return_value=('overwrite', False)):
            self.panel._clipboard_paste_into(self.target)
        self._assert_off_gui(seen, 'shutil.rmtree')
        self.assertFalse(os.path.exists(os.path.join(dst, 'old.txt')))
        self.assertTrue(os.path.isfile(os.path.join(dst, 'f.txt')))

    def test_overwrite_failure_is_reported_not_copied(self):
        """覆盖前的 rmtree 失败：条目记为失败，不会再往已存在的目标上复制。"""
        dst = os.path.join(self.target, 'srcdir')
        os.makedirs(dst)
        explorer_clipboard.set_items([("local", self.src)])
        with mock.patch.object(shutil, 'rmtree', side_effect=OSError('busy')), \
                mock.patch.object(explorer_common, 'resolve_paste_conflict',
                                  return_value=('overwrite', False)), \
                mock.patch.object(QMessageBox, 'warning') as warn:
            self.panel._clipboard_paste_into(self.target)
        self.assertTrue(warn.called, "覆盖失败没有报错")
        self.assertFalse(os.path.exists(os.path.join(dst, 'f.txt')))


if __name__ == '__main__':
    unittest.main()
