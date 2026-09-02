# -*- coding: utf-8 -*-
"""编辑窗格内文件标签（与终端标签同型）的行为测试。

设计要点：
- 标签路径以 QTabBar tabData 为唯一事实来源（拖动重排 tabData 跟随，不会错位）；
- 切换/关闭标签完全复用 open_file/_close_editor 既有机制（脏缓冲提示、
  视图状态记忆、崩溃恢复备份天然生效）；
- 只有当前文件可能有未保存改动 → 非当前标签可以直接摘掉；
- 标签数 ≥2 才显示标签条，单文件界面与原来一致。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_editor_tabs.py -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication, QMessageBox


def _shutdown_panes(panes, app):
    """销毁窗格前先停掉文件监视与各类定时器，再彻底消化 DeferredDelete。

    否则 tearDown 随后删除临时目录时，QFileSystemWatcher 会在半销毁的
    窗格上触发 _handle_external_change（内含模态弹窗）——CI 上表现为
    Windows 堆损坏崩溃 / 各平台测试进程挂死（v1.17.7 首次打 tag 实翻）。
    """
    from PyQt6.QtCore import QEvent
    for pane in panes:
        try:
            pane._stop_watching()
            pane._autosave_timer.stop()
            timer = getattr(pane, '_external_change_timer', None)
            if timer is not None:
                timer.stop()
            pane.deleteLater()
        except RuntimeError:
            pass  # 窗格已被销毁
    for _ in range(5):
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from file_editor import FileEditorWidget
        self.pane = FileEditorWidget(theme={})
        self.pane.resize(800, 600)
        self._tmp = tempfile.TemporaryDirectory()
        self.a = str(Path(self._tmp.name) / 'a.txt')
        self.b = str(Path(self._tmp.name) / 'b.txt')
        Path(self.a).write_text('content-a', encoding='utf-8')
        Path(self.b).write_text('content-b', encoding='utf-8')

    def tearDown(self):
        _shutdown_panes([self.pane], self.app)
        self._tmp.cleanup()

    def _paths(self):
        return self.pane._tab_paths()


class TestEditorFileTabs(_Base):
    def test_open_files_creates_tabs(self):
        p = self.pane
        self.assertTrue(p.open_file(self.a))
        self.assertEqual(self._paths(), [self.a])
        self.assertFalse(p.tab_bar.isVisible(), "单文件不显示标签条")
        self.assertTrue(p.open_file(self.b))
        self.assertEqual(self._paths(), [self.a, self.b])
        self.assertEqual(p.tab_bar.currentIndex(), 1)
        self.assertEqual(p.get_current_file(), self.b)

    def test_reopen_existing_no_duplicate(self):
        p = self.pane
        p.open_file(self.a)
        p.open_file(self.b)
        p.open_file(self.a)
        self.assertEqual(self._paths(), [self.a, self.b])
        self.assertEqual(p.tab_bar.currentIndex(), 0)
        self.assertEqual(p.get_current_file(), self.a)

    def test_switch_tab_loads_file(self):
        p = self.pane
        p.open_file(self.a)
        p.open_file(self.b)
        p.tab_bar.setCurrentIndex(0)   # 模拟点击 a 的标签
        self.assertEqual(p.get_current_file(), self.a)
        self.assertEqual(p.editor.toPlainText(), 'content-a')

    def test_close_current_tab_switches_to_neighbor(self):
        p = self.pane
        closed = []
        p.editor_closed.connect(lambda: closed.append(1))
        p.open_file(self.a)
        p.open_file(self.b)
        p._close_editor()   # 关当前(b) → 切到 a，窗格保留
        self.assertEqual(self._paths(), [self.a])
        self.assertEqual(p.get_current_file(), self.a)
        self.assertEqual(p.editor.toPlainText(), 'content-a')
        self.assertEqual(closed, [], "多标签关闭不应触发窗格关闭")

    def test_close_last_tab_closes_pane(self):
        p = self.pane
        closed = []
        p.editor_closed.connect(lambda: closed.append(1))
        p.open_file(self.a)
        p._close_editor()
        self.assertEqual(self._paths(), [])
        self.assertIsNone(p.get_current_file())
        self.assertEqual(closed, [1], "最后一个标签应走原有关闭窗格路径")

    def test_close_noncurrent_tab_keeps_current(self):
        p = self.pane
        p.open_file(self.a)
        p.open_file(self.b)
        p._on_file_tab_close_requested(0)   # 关掉非当前的 a
        self.assertEqual(self._paths(), [self.b])
        self.assertEqual(p.get_current_file(), self.b)
        self.assertEqual(p.editor.toPlainText(), 'content-b')

    def test_dirty_switch_discard(self):
        p = self.pane
        p.open_file(self.a)
        p.open_file(self.b)
        p.editor.setPlainText('modified-b')
        with patch.object(QMessageBox, 'question',
                          return_value=QMessageBox.StandardButton.Discard):
            p.tab_bar.setCurrentIndex(0)
        self.assertEqual(p.get_current_file(), self.a)
        # 丢弃后 b 落盘内容不变
        self.assertEqual(Path(self.b).read_text(encoding='utf-8'), 'content-b')

    def test_dirty_switch_cancel_reverts_selection(self):
        p = self.pane
        p.open_file(self.a)
        p.open_file(self.b)
        p.editor.setPlainText('modified-b')
        with patch.object(QMessageBox, 'question',
                          return_value=QMessageBox.StandardButton.Cancel):
            p.tab_bar.setCurrentIndex(0)
        self.assertEqual(p.get_current_file(), self.b, "取消后应停留在原文件")
        self.assertEqual(p.tab_bar.currentIndex(), 1, "取消后标签选择应回弹")
        self.assertEqual(p.editor.toPlainText(), 'modified-b',
                         "取消后未保存修改必须原样保留")

    def test_save_as_retargets_tab(self):
        from PyQt6.QtWidgets import QFileDialog
        p = self.pane
        p.open_file(self.a)
        p.open_file(self.b)
        c = str(Path(self._tmp.name) / 'c.txt')
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(c, '')):
            self.assertTrue(p.save_file_as())
        self.assertEqual(self._paths(), [self.a, c])
        self.assertEqual(p.get_current_file(), c)
        self.assertEqual(p.tab_bar.tabText(1), 'c.txt')

    def test_tab_reorder_keeps_binding(self):
        p = self.pane
        p.open_file(self.a)
        p.open_file(self.b)
        p.tab_bar.moveTab(1, 0)    # b 挪到最前（tabData 跟随标签）
        self.assertEqual(self._paths(), [self.b, self.a])
        p.tab_bar.setCurrentIndex(1)
        self.assertEqual(p.get_current_file(), self.a)


class TestEditorAreaWithTabs(unittest.TestCase):
    """EditorArea 集成：Explorer 打开文件落到活动窗格的标签；分屏互不影响。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from file_editor import EditorArea
        self.area = EditorArea(theme={})
        self.area.resize(1000, 700)
        self._tmp = tempfile.TemporaryDirectory()
        self.a = str(Path(self._tmp.name) / 'a.txt')
        self.b = str(Path(self._tmp.name) / 'b.txt')
        Path(self.a).write_text('aa', encoding='utf-8')
        Path(self.b).write_text('bb', encoding='utf-8')

    def tearDown(self):
        _shutdown_panes(list(self.area.panes), self.app)
        self.area.deleteLater()
        self.app.processEvents()
        self._tmp.cleanup()

    def test_open_in_active_accumulates_tabs(self):
        area = self.area
        area.open_file_in_active(self.a)
        area.open_file_in_active(self.b)
        pane = area.active_pane
        self.assertEqual(pane._tab_paths(), [self.a, self.b])

    def test_split_pane_starts_with_single_tab(self):
        from PyQt6.QtCore import Qt
        area = self.area
        area.open_file_in_active(self.a)
        area.open_file_in_active(self.b)
        pane = area.active_pane
        area.split_pane(pane, Qt.Orientation.Horizontal)
        new_pane = area.active_pane
        self.assertIsNot(new_pane, pane)
        # 新窗格复制当前文件，且只带一个标签；原窗格标签不受影响
        self.assertEqual(new_pane._tab_paths(), [self.b])
        self.assertEqual(pane._tab_paths(), [self.a, self.b])


if __name__ == '__main__':
    unittest.main()


class TestTabContextMenu(_Base):
    """标签右键菜单：关闭 / 关闭其他 / 关闭左侧 / 关闭右侧。

    批量关闭按**路径**做，不按索引 —— 每关掉一个，后面的索引就往前挪，
    按索引删必然错位关错文件。
    """

    def _four_files(self):
        paths = []
        for name in ("t0.txt", "t1.txt", "t2.txt", "t3.txt"):
            p = str(Path(self._tmp.name) / name)
            Path(p).write_text(name, encoding='utf-8')
            paths.append(p)
        for p_ in paths:
            self.pane.open_file(p_)
            self.app.processEvents()
        return paths

    def _menu_actions(self, tab_index):
        """在第 tab_index 个标签上模拟右键，返回 {动作文案: QAction}。

        exec 会阻塞，所以把它替成"记下菜单、返回 None"。
        """
        from PyQt6.QtCore import QPoint, Qt as QtCore_Qt
        from PyQt6.QtGui import QContextMenuEvent
        from PyQt6.QtWidgets import QMenu

        seen = {}
        orig_exec = QMenu.exec

        def fake_exec(menu, *a, **k):
            seen["menu"] = menu
            seen["actions"] = {act.text(): act for act in menu.actions()}
            return None
        QMenu.exec = fake_exec
        try:
            bar = self.pane.tab_bar
            rect = bar.tabRect(tab_index)
            ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                   rect.center(),
                                   bar.mapToGlobal(rect.center()))
            bar.contextMenuEvent(ev)
        finally:
            QMenu.exec = orig_exec
        return seen.get("actions", {}), seen.get("menu")

    def _trigger(self, tab_index, key):
        """真按下菜单里的某一项（不是直接调内部方法）。"""
        from i18n import t
        from PyQt6.QtWidgets import QMenu
        label = t(key)
        orig_exec = QMenu.exec

        def fake_exec(menu, *a, **k):
            for act in menu.actions():
                if act.text() == label:
                    return act
            return None
        QMenu.exec = fake_exec
        try:
            from PyQt6.QtGui import QContextMenuEvent
            bar = self.pane.tab_bar
            rect = bar.tabRect(tab_index)
            ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse,
                                   rect.center(),
                                   bar.mapToGlobal(rect.center()))
            bar.contextMenuEvent(ev)
        finally:
            QMenu.exec = orig_exec
        self.app.processEvents()

    def test_close_others_keeps_only_the_clicked_tab(self):
        paths = self._four_files()
        self._trigger(1, "tab.close_others")
        self.assertEqual(self._paths(), [paths[1]])

    def test_close_to_the_left(self):
        paths = self._four_files()
        self._trigger(2, "tab.close_left")
        self.assertEqual(self._paths(), paths[2:])

    def test_close_to_the_right(self):
        paths = self._four_files()
        self._trigger(1, "tab.close_right")
        self.assertEqual(self._paths(), paths[:2])

    def test_close_single_tab(self):
        paths = self._four_files()
        self._trigger(0, "tab.close")
        self.assertEqual(self._paths(), paths[1:])

    def test_closing_left_of_the_first_tab_is_disabled(self):
        self._four_files()
        actions, _menu = self._menu_actions(0)
        from i18n import t
        self.assertFalse(actions[t("tab.close_left")].isEnabled(),
                         "第一个标签左边没东西可关")
        self.assertTrue(actions[t("tab.close_right")].isEnabled())

    def test_close_others_survives_index_shifting(self):
        """关掉 3 个的过程中索引一直在挪：按路径做才不会关错。"""
        paths = self._four_files()
        self._trigger(3, "tab.close_others")
        self.assertEqual(self._paths(), [paths[3]])
        self.assertEqual(self.pane._current_file, paths[3])
