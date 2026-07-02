"""远程主机列表排序：按别名 / 按主机名 / 手动（拖拽顺序持久化）。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_remote_host_sort -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 配置读写经 app_config 单点；重定向到临时文件，避免污染真实配置、
        # 也避免用例间通过共享配置互相串扰（host_order 持久化）
        import app_config
        self._tmp_cfg = Path(tempfile.mkdtemp()) / "cfg.json"
        self._orig_get_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._tmp_cfg

    def tearDown(self):
        import app_config
        app_config.get_config_path = self._orig_get_path

    def _panel(self, hosts):
        from remote_explorer_widget import RemoteExplorerPanel
        panel = RemoteExplorerPanel(theme={})
        panel._hosts = list(hosts)
        panel._extra_hosts = []
        return panel

    @staticmethod
    def _hosts():
        from ssh_session import HostConfig
        return [
            HostConfig(alias="zebra", hostname="10.0.0.9", user="a", port=22),
            HostConfig(alias="alpha", hostname="10.0.0.2", user="b", port=22),
            HostConfig(alias="mid", hostname="10.0.0.5", user="c", port=2222),
        ]


class TestHostSort(_Base):
    def test_sort_by_alias(self):
        p = self._panel(self._hosts())
        p._host_sort = "alias"
        self.assertEqual([h.alias for h in p._sorted_hosts(p._hosts)],
                         ["alpha", "mid", "zebra"])

    def test_sort_by_host(self):
        p = self._panel(self._hosts())
        p._host_sort = "host"
        # 按 hostname 升序：10.0.0.2 < 10.0.0.5 < 10.0.0.9
        self.assertEqual([h.alias for h in p._sorted_hosts(p._hosts)],
                         ["alpha", "mid", "zebra"])

    def test_manual_without_saved_order_keeps_original(self):
        p = self._panel(self._hosts())
        p._host_sort = "manual"
        self.assertEqual([h.alias for h in p._sorted_hosts(p._hosts)],
                         ["zebra", "alpha", "mid"])

    def test_manual_uses_saved_order_unknown_to_end(self):
        p = self._panel(self._hosts())
        p._host_sort = "manual"
        # 只记了两个的顺序，未记录的 'mid' 应排到末尾
        p._save_host_order(["alpha", "zebra"])
        self.assertEqual([h.alias for h in p._sorted_hosts(p._hosts)],
                         ["alpha", "zebra", "mid"])

    def test_reorder_persists_current_list_order(self):
        p = self._panel(self._hosts())
        p._host_sort = "manual"
        p._save_host_order(["mid", "zebra", "alpha"])
        p._populate_hosts_list()
        # 模拟拖放完成后的回调：把当前可视顺序存成手动顺序
        p._on_hosts_reordered()
        self.assertEqual(p._load_host_order(), ["mid", "zebra", "alpha"])

    def test_reorder_ignored_when_not_manual(self):
        p = self._panel(self._hosts())
        p._host_sort = "alias"
        p._save_host_order(["mid", "zebra", "alpha"])
        p._populate_hosts_list()
        # 非 manual 模式下拖拽回调不应覆盖已保存的手动顺序
        p._on_hosts_reordered()
        self.assertEqual(p._load_host_order(), ["mid", "zebra", "alpha"])


if __name__ == "__main__":
    unittest.main()
