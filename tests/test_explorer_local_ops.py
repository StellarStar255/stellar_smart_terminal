# -*- coding: utf-8 -*-
"""本地 Explorer 复制 / 删除 / 改名的回归测试。

审查发现三处问题：
1. `shutil.copytree(src, dst)` 默认跟随软链：目录里一个 `link -> ..` 就会一路
   复制到 ENAMETOOLONG 才停，指向 `/` 的软链会试图复制整块盘；且整个复制在
   GUI 线程同步跑，大目录期间界面完全无响应。
2. macOS 批量删除逐个文件起一次 `osascript`（各 20s 超时），删 50 个文件
   = 50 次 Finder AppleScript 往返，全部在 GUI 线程。
3. 文件树里改名 / 删除后编辑器不知情（继续按旧路径自动保存产生两份文件）；
   面板没有任何"文件已改名/已删除"的信号可供外部订阅。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_local_ops.py -v
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

from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

import explorer_widget
from explorer_widget import ExplorerPanel


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='explorer_ops_'))
        self.panel = ExplorerPanel()
        self.panel.set_root_path(self.tmp)
        self.app.processEvents()

    def tearDown(self):
        self.panel.deleteLater()
        for _ in range(3):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCopyOffThreadAndSymlinks(_Base):
    @unittest.skipIf(sys.platform == 'win32', 'symlink 需要特权，Windows 不测')
    def test_symlink_loop_is_copied_as_link_not_recursed(self):
        """目录里 `link -> ..`：复制结果里 link 必须仍是软链，不能展开递归"""
        src = os.path.join(self.tmp, 'src')
        os.makedirs(src)
        open(os.path.join(src, 'a.txt'), 'w').close()
        os.symlink('..', os.path.join(src, 'loop'))
        target = os.path.join(self.tmp, 'target')
        os.makedirs(target)

        self.panel._handle_drop_copy([src], target)

        dst = os.path.join(target, 'src')
        self.assertTrue(os.path.isfile(os.path.join(dst, 'a.txt')))
        self.assertTrue(os.path.islink(os.path.join(dst, 'loop')),
                        "软链被当成真目录递归复制了")
        # 复制的是链接本身（指向 ..），不是展开后的物理目录
        self.assertEqual(os.readlink(os.path.join(dst, 'loop')), '..')

    def test_copytree_runs_off_gui_thread(self):
        """拖入复制的 copytree 必须在工作线程执行，不能卡 GUI 线程"""
        src = os.path.join(self.tmp, 'srcdir')
        os.makedirs(src)
        open(os.path.join(src, 'f'), 'w').close()
        target = os.path.join(self.tmp, 'tgt')
        os.makedirs(target)
        seen = []
        orig = shutil.copytree

        def spy(*a, **k):
            seen.append(threading.get_ident())
            return orig(*a, **k)

        with mock.patch.object(shutil, 'copytree', side_effect=spy):
            self.panel._handle_drop_copy([src], target)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], threading.main_thread().ident,
                            "copytree 仍在 GUI 线程上执行")
        self.assertTrue(os.path.isfile(os.path.join(target, 'srcdir', 'f')))

    def test_paste_copy_runs_off_gui_thread(self):
        """粘贴路径同样：本地复制在工作线程"""
        import explorer_clipboard
        src = os.path.join(self.tmp, 'p_src')
        os.makedirs(src)
        open(os.path.join(src, 'f'), 'w').close()
        target = os.path.join(self.tmp, 'p_tgt')
        os.makedirs(target)
        explorer_clipboard.set_items([("local", src)], push_local_paths=[src])
        seen = []
        orig = shutil.copytree

        def spy(*a, **k):
            seen.append((threading.get_ident(), k.get('symlinks')))
            return orig(*a, **k)

        try:
            with mock.patch.object(shutil, 'copytree', side_effect=spy):
                self.panel._clipboard_paste_into(target)
        finally:
            explorer_clipboard.clear()
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0][0], threading.main_thread().ident)
        self.assertTrue(seen[0][1], "粘贴的 copytree 没传 symlinks=True")
        self.assertTrue(os.path.isfile(os.path.join(target, 'p_src', 'f')))


class TestMacTrashBatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_applescript_quotes_paths(self):
        """路径里的双引号和反斜杠必须转义，否则脚本被截断"""
        p1 = '/tmp/we"ird\\name'
        p2 = '/tmp/plain'
        script = ExplorerPanel._applescript_trash_script([p1, p2])
        self.assertIn('POSIX file "/tmp/we\\"ird\\\\name"', script)
        self.assertIn('POSIX file "/tmp/plain"', script)
        self.assertIn('tell application "Finder" to delete', script)

    def test_batch_is_one_osascript_call(self):
        """N 个路径 = 一次 osascript，而不是 N 次"""
        paths = [f'/tmp/x{i}' for i in range(5)]
        with mock.patch.object(explorer_widget.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0)
            ok = ExplorerPanel._macos_trash_via_finder(paths)
        self.assertTrue(ok)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], 'osascript')
        for p in paths:
            self.assertIn(p, argv[2])


class TestDeleteSignalsAndThread(_Base):
    def test_delete_runs_off_gui_thread_and_emits_file_deleted(self):
        f1 = os.path.join(self.tmp, 'd1.txt')
        f2 = os.path.join(self.tmp, 'd2.txt')
        for f in (f1, f2):
            open(f, 'w').close()
        deleted = []
        self.panel.file_deleted.connect(deleted.append)
        seen = []

        def fake_batch(paths):
            seen.append(threading.get_ident())
            for p in paths:
                os.remove(p)
            return []

        with mock.patch.object(self.panel, '_send_to_trash_batch', side_effect=fake_batch), \
             mock.patch.object(QMessageBox, 'question',
                               return_value=QMessageBox.StandardButton.Yes):
            self.panel._delete_paths([f1, f2])

        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], threading.main_thread().ident,
                            "删除仍在 GUI 线程上执行")
        self.assertEqual(sorted(deleted), sorted([f1, f2]))


class TestRenameSignal(_Base):
    def test_model_rename_emits_file_renamed_with_full_paths(self):
        got = []
        self.panel.file_renamed.connect(lambda o, n: got.append((o, n)))
        self.panel.model.fileRenamed.emit(self.tmp, 'old.txt', 'new.txt')
        self.assertEqual(got, [(os.path.join(self.tmp, 'old.txt'),
                                os.path.join(self.tmp, 'new.txt'))])

    def test_rename_signal_survives_refresh_model_rebuild(self):
        """refresh() 会重建模型，新模型也必须接上"""
        got = []
        self.panel.file_renamed.connect(lambda o, n: got.append((o, n)))
        self.panel.refresh()
        self.app.processEvents()
        self.panel.model.fileRenamed.emit(self.tmp, 'a', 'b')
        self.assertEqual(len(got), 1)


if __name__ == '__main__':
    unittest.main()
