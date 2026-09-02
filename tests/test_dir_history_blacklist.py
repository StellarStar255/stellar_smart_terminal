"""工作目录历史「删除后复活」回归测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_dir_history_blacklist.py -v

背景（两次同类事故）：
- mac 打包 app 从 Dock 启动 cwd='/'，启动时自动加入历史 → 删掉的 '/' 复活；
- Ubuntu 从桌面启动 cwd=$HOME（如 /home/zy），同一段自动加入逻辑 → 删掉的
  家目录每次重启都复活。
修法：显式删除的路径进持久黑名单（配置键 working_dir_removed），启动
自动加入与多窗口保存合并都跳过黑名单；用户显式选回则解除拉黑。

不实例化 MainWindow（太重、CI 上有析构残留崩溃史），用假对象借调
_load_config / _merge_dir_history_for_save 这两个纯数据方法。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


class TestDirHistoryBlacklist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # main_window 必须晚于 QApplication 导入（模块级导入会在无 app 时崩）
        global app_config, main_window
        import app_config
        import main_window

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='dir_history_test_')
        self._cfg_path = Path(self._tmp.name) / 'config.json'
        self._orig_get_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._cfg_path

    def tearDown(self):
        app_config.get_config_path = self._orig_get_path
        self._tmp.cleanup()

    def _write_cfg(self, **kwargs):
        cfg = {'split_spring_default_on_migrated': True}  # 跳过一次性迁移写盘
        cfg.update(kwargs)
        self._cfg_path.write_text(json.dumps(cfg), encoding='utf-8')

    def _make_fake(self):
        MW = main_window.MainWindow

        class _Fake:
            THEMES = MW.THEMES
            DEFAULT_ALERT_PATTERNS = MW.DEFAULT_ALERT_PATTERNS
            # host_class(self) 在假 self 上回退到本类：给它进程级共享属性的替身
            _navigator_dock_mode = 'embed'
            _left_width_by_screen = {}
            _sidebar_height_sync = False
            _clamp_scrollback = staticmethod(MW._clamp_scrollback)
            _init_config_state = MW._init_config_state
            _init_explorer_state = MW._init_explorer_state
            _init_remote_state = MW._init_remote_state
            _load_config = MW._load_config
            _merge_dir_history_for_save = MW._merge_dir_history_for_save
            _add_to_dir_history = MW._add_to_dir_history

            def _populate_working_dirs(self):
                pass

            def _save_config(self):
                pass

        fake = _Fake()
        # _load_config 读盘时用各 mixin 的默认值兜底，先按真实顺序初始化状态
        fake._init_explorer_state()
        fake._init_remote_state()
        # 指向临时配置：避免 _load_config 里的旧配置迁移逻辑碰真实 home 目录
        fake.CONFIG_FILE = self._cfg_path
        return fake

    # ---- 启动时的自动加入 ----

    def test_startup_auto_adds_cwd(self):
        """未被拉黑时，启动仍会把 cwd 自动加入历史（原有特性不回退）"""
        self._write_cfg(working_dir_history=['/some/project'],
                        working_dir_freq={'/some/project': 3})
        w = self._make_fake()
        w._load_config()
        self.assertIn(os.getcwd(), w.working_dir_history)

    def test_startup_does_not_resurrect_deleted_cwd(self):
        """核心回归：cwd（如 Ubuntu 桌面启动的 /home/zy）已被显式删除，
        重启后不得被自动加入复活"""
        cwd = os.getcwd()
        self._write_cfg(working_dir_history=['/some/project'],
                        working_dir_freq={'/some/project': 3},
                        working_dir_removed=[cwd])
        w = self._make_fake()
        w._load_config()
        self.assertNotIn(cwd, w.working_dir_history)
        self.assertIn(cwd, w._dir_history_removed)

    def test_startup_filters_blacklisted_entries_from_history(self):
        """其它窗口把已拉黑路径写回 history 时，加载要防御性过滤"""
        self._write_cfg(working_dir_history=['/home/zy', '/some/project'],
                        working_dir_freq={'/home/zy': 9, '/some/project': 1},
                        working_dir_removed=['/home/zy'])
        w = self._make_fake()
        w._load_config()
        self.assertNotIn('/home/zy', w.working_dir_history)

    def test_startup_purges_fs_root(self):
        """旧问题不回退：历史里的 '/' 被清出并记入黑名单"""
        self._write_cfg(working_dir_history=['/', '/some/project'],
                        working_dir_freq={'/': 5, '/some/project': 1})
        w = self._make_fake()
        w._load_config()
        self.assertNotIn('/', w.working_dir_history)
        self.assertIn('/', w._dir_history_removed)

    # ---- 保存合并 ----

    def test_merge_purges_removed_from_disk_union(self):
        """本窗口删除的路径在保存合并时从磁盘并集中剔除，黑名单保留待落盘"""
        self._write_cfg(working_dir_history=['/home/zy', '/some/project'],
                        working_dir_freq={'/home/zy': 9, '/some/project': 1})
        w = self._make_fake()
        w.working_dir_history = ['/some/project']
        w._working_dir_freq = {'/some/project': 1}
        w._dir_history_removed = {'/home/zy'}
        w._dir_history_readded = set()
        w._merge_dir_history_for_save()
        self.assertNotIn('/home/zy', w.working_dir_history)
        self.assertIn('/some/project', w.working_dir_history)
        self.assertIn('/home/zy', w._dir_history_removed)

    def test_merge_propagates_removal_across_windows(self):
        """窗口 A 删除并落盘黑名单后，仍在内存里持有该路径的窗口 B
        保存时不得把它复活"""
        self._write_cfg(working_dir_history=['/some/project'],
                        working_dir_freq={'/some/project': 1},
                        working_dir_removed=['/home/zy'])
        w = self._make_fake()  # 窗口 B：删除发生前加载，内存里还有 zy
        w.working_dir_history = ['/home/zy', '/some/project']
        w._working_dir_freq = {'/home/zy': 9, '/some/project': 1}
        w._dir_history_removed = set()
        w._dir_history_readded = set()
        w._merge_dir_history_for_save()
        self.assertNotIn('/home/zy', w.working_dir_history)
        self.assertIn('/home/zy', w._dir_history_removed)

    def test_explicit_readd_lifts_blacklist(self):
        """删除后用户又显式选回：解除拉黑，且不被磁盘黑名单重新压掉"""
        self._write_cfg(working_dir_history=['/some/project'],
                        working_dir_freq={'/some/project': 1},
                        working_dir_removed=['/home/zy'])
        w = self._make_fake()
        w._load_config()
        w._add_to_dir_history('/home/zy')  # 用户通过 Browse/下拉显式选回
        self.assertIn('/home/zy', w.working_dir_history)
        w._merge_dir_history_for_save()
        self.assertIn('/home/zy', w.working_dir_history)
        self.assertNotIn('/home/zy', w._dir_history_removed)


if __name__ == '__main__':
    unittest.main()
