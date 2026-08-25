# -*- coding: utf-8 -*-
"""工作区恢复（正常退出 → 重启找回窗口/标签布局）的测试

覆盖：
- _collect_windows_snapshot 含标签页（目录/自定义名/当前索引）
- restore_workspace_on_start：套用快照恢复标签结构（不自动起会话）、
  设置关闭时零动作、快照缺失/畸形零副作用
- 升级恢复优先：restore_windows_after_update 消费成功时返回 True
  （app.py 据此跳过工作区恢复）
- _restore_tabs_from_snapshot：目录失效的标签跳过 cwd、自定义名恢复

配置 patch app_config.get_config_path 到临时文件（既定隔离约定）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_workspace_restore.py -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


class TestWorkspaceRestore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        global app_config, main_window
        import app_config
        import main_window
        cls.w = main_window.MainWindow()
        cls.w.show()
        cls.app.processEvents()

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtCore import QEvent
        cls.w.close()
        cls.w.deleteLater()
        del cls.w
        for _ in range(5):
            cls.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            cls.app.processEvents()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='ws_restore_')
        self._cfg_path = Path(self._tmp.name) / 'config.json'
        self._cfg_path.write_text('{}')
        self._orig_get_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._cfg_path
        # 收缩到单标签基线
        w = self.w
        while w.tab_widget.count() > 1:
            w._close_tab(w.tab_widget.count() - 1, auto_create_new=False)
        self.app.processEvents()

    def tearDown(self):
        app_config.get_config_path = self._orig_get_path
        self._tmp.cleanup()

    def _write_cfg(self, d):
        self._cfg_path.write_text(json.dumps(d), encoding='utf-8')

    def _read_cfg(self):
        return json.loads(self._cfg_path.read_text(encoding='utf-8'))

    def test_snapshot_contains_tabs(self):
        w = self.w
        idx = w._add_new_tab(tab_name='我的标签', tab_cwd=self._tmp.name)
        page = w.tab_widget.widget(idx)
        page._custom_tab_name = '我的标签'
        try:
            entries = main_window.MainWindow._collect_windows_snapshot()
            mine = [e for e in entries if len(e.get('tabs', [])) >= 2]
            self.assertTrue(mine, f"未采集到多标签窗口: {entries}")
            tabs = mine[0]['tabs']
            self.assertEqual(tabs[-1]['name'], '我的标签')
            self.assertEqual(tabs[-1]['cwd'], self._tmp.name)
            self.assertIn('current_tab', mine[0])
        finally:
            w._close_tab(idx, auto_create_new=False)

    def test_restore_applies_current_tab_only(self):
        """重启后不再重建多标签（会话无法恢复，空标签没用——用户点名去掉）。

        只把「重启前正在看的那个标签」的目录/自定义名套到现有单标签上。
        """
        w = self.w
        os.makedirs(Path(self._tmp.name) / 'proj2', exist_ok=True)
        self._write_cfg({'workspace_snapshot': {
            'ts': time.time(),
            'windows': [{
                'cwd': self._tmp.name,
                'geometry': [60, 80, 1360, 820],
                'maximized': False,
                'tabs': [
                    {'cwd': self._tmp.name, 'name': ''},
                    {'cwd': str(Path(self._tmp.name) / 'proj2'), 'name': '第二个'},
                    {'cwd': '/nonexistent/xyz', 'name': ''},
                ],
                'current_tab': 1,
            }],
        }})
        try:
            main_window.MainWindow.restore_workspace_on_start(w)
            self.app.processEvents()
            # 不重建多标签：只剩启动时的那一个
            self.assertEqual(w.tab_widget.count(), 1,
                             "重启后不应重建成排的空标签")
            # 但重启前正在看的标签的目录/自定义名要套到现有标签上
            self.assertEqual(w.tab_cwds.get(0), str(Path(self._tmp.name) / 'proj2'))
            self.assertEqual(w.tab_widget.tabText(0), '第二个')
            # 会话不自动启动
            for terms in w.tab_terminals.values():
                for term in terms:
                    self.assertFalse(term.is_running())
        finally:
            page0 = w.tab_widget.widget(0)
            if page0 is not None:
                page0._custom_tab_name = None
            while w.tab_widget.count() > 1:
                w._close_tab(w.tab_widget.count() - 1, auto_create_new=False)

    def test_restore_skips_invalid_current_cwd(self):
        """当前标签目录已失效时不套用失效 cwd，也不崩溃。"""
        w = self.w
        self._write_cfg({'workspace_snapshot': {
            'ts': time.time(),
            'windows': [{
                'cwd': self._tmp.name,
                'geometry': [60, 80, 1360, 820],
                'maximized': False,
                'tabs': [{'cwd': '/nonexistent/xyz', 'name': ''}],
                'current_tab': 0,
            }],
        }})
        main_window.MainWindow.restore_workspace_on_start(w)
        self.app.processEvents()
        self.assertEqual(w.tab_widget.count(), 1)
        # 失效目录不得被套用（窗口级合法 cwd 照常生效）
        self.assertNotEqual(w.tab_cwds.get(0), '/nonexistent/xyz')

    def test_disabled_or_missing_snapshot_noop(self):
        w = self.w
        before = w.tab_widget.count()
        # 无快照
        main_window.MainWindow.restore_workspace_on_start(w)
        self.assertEqual(w.tab_widget.count(), before)
        # 有快照但设置关闭
        self._write_cfg({
            'workspace_restore_enabled': False,
            'workspace_snapshot': {'ts': time.time(), 'windows': [{
                'cwd': self._tmp.name, 'geometry': [0, 0, 800, 600],
                'maximized': False,
                'tabs': [{'cwd': self._tmp.name, 'name': 'x'}] * 3,
                'current_tab': 0,
            }]},
        })
        main_window.MainWindow.restore_workspace_on_start(w)
        self.assertEqual(w.tab_widget.count(), before)

    def test_update_restore_returns_true_and_wins(self):
        w = self.w
        self._write_cfg({'update_restore_windows': {
            'ts': time.time(),
            'windows': [{'cwd': self._tmp.name,
                         'geometry': [60, 80, 1360, 820], 'maximized': False}],
        }})
        self.assertTrue(main_window.MainWindow.restore_windows_after_update(w))
        # 快照被一次性消费
        self.assertNotIn('update_restore_windows', self._read_cfg())
        # 无快照时返回 False（app.py 据此走工作区恢复）
        self.assertFalse(main_window.MainWindow.restore_windows_after_update(w))

    def test_checkpoint_writes_snapshot(self):
        w = self.w
        w._write_workspace_snapshot()
        snap = self._read_cfg().get('workspace_snapshot')
        self.assertIsInstance(snap, dict)
        self.assertTrue(snap.get('windows'))
        self.assertIn('tabs', snap['windows'][0])

    def test_reopened_window_geometry_reasserted(self):
        """次窗口 show 后被系统（台前调度等）推挪时，校正循环应拉回快照几何。

        主窗口不受此害（show 后才套几何），历史 bug：其余窗口 show 前
        setGeometry 一次就不管了，恢复后初始大小全偏。
        """
        w = self.w
        # 尺寸需在 MainWindow 最小尺寸(1000x700)之上，否则被夹住
        target = [80, 90, 1120, 760]
        entry = {'cwd': '', 'geometry': target, 'maximized': False}
        win = w._open_restored_window(entry)
        try:
            self.app.processEvents()  # 让 0ms 的首次断言先跑掉
            # 模拟系统在 show 后异步推挪/压窄窗口
            win.setGeometry(200, 210, 1000, 700)
            deadline = time.time() + 3
            while time.time() < deadline:
                self.app.processEvents()
                g = win.geometry()
                if [g.x(), g.y(), g.width(), g.height()] == target:
                    break
                time.sleep(0.05)
            g = win.geometry()
            self.assertEqual([g.x(), g.y(), g.width(), g.height()], target)
        finally:
            from PyQt6.QtCore import QEvent
            w.detached_windows.remove(win)
            win.close()
            win.deleteLater()
            for _ in range(5):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
