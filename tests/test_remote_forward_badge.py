"""远程主机列表要标出「这台主机上正挂着端口转发」。

转发挂在 ssh 主连接（control socket）上，进程级；登记也按进程共享，
多窗口的主机列表都能看到。主连接的 socket 没了 → 转发必然没了 → 标记
自动消失、登记清掉。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_remote_forward_badge.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication  # noqa: E402

import ssh_control  # noqa: E402
from ssh_session import HostConfig  # noqa: E402

_RULE = {"type": "L", "bind_host": "127.0.0.1", "bind_port": "8080",
         "dest_host": "127.0.0.1", "dest_port": "80"}


class RemoteForwardBadgeTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import remote_explorer_widget as rew
        self.rew = rew
        rew._ACTIVE_FORWARDS.clear()
        self.addCleanup(rew._ACTIVE_FORWARDS.clear)
        self.hosts = [
            HostConfig(alias="alpha", hostname="10.0.0.2", user="b", port=22),
            HostConfig(alias="zebra", hostname="10.0.0.9", user="a", port=22),
        ]

    def _panel(self):
        panel = self.rew.RemoteExplorerPanel(theme={})
        panel._hosts = list(self.hosts)
        panel._extra_hosts = []
        panel._load_forwards = lambda alias: [dict(_RULE)] if alias == "alpha" else []
        self.addCleanup(panel.deleteLater)
        return panel

    @staticmethod
    def _texts(panel):
        lst = panel._hosts_list
        return {lst.item(i).data(self_role()).alias: lst.item(i)
                for i in range(lst.count())}

    def test_badge_shown_for_host_with_active_forward(self):
        panel = self._panel()
        key = self.rew._ForwardsDialog.rule_key(_RULE)
        panel._active_forwards["alpha"] = {key}
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=True):
            panel._populate_hosts_list()
        items = self._texts(panel)
        self.assertIn("🔀 1", items["alpha"].text())
        self.assertIn("8080", items["alpha"].toolTip())
        self.assertNotIn("🔀", items["zebra"].text())
        self.assertEqual(items["zebra"].toolTip(), "")

    def test_badge_gone_when_master_socket_missing(self):
        panel = self._panel()
        panel._active_forwards["alpha"] = {self.rew._ForwardsDialog.rule_key(_RULE)}
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=False):
            panel._populate_hosts_list()
        self.assertNotIn("🔀", self._texts(panel)["alpha"].text())
        self.assertNotIn("alpha", panel._active_forwards)   # 陈旧登记被清掉

    def test_registry_shared_across_panels(self):
        a = self._panel()
        b = self._panel()
        a._active_forwards["alpha"] = {self.rew._ForwardsDialog.rule_key(_RULE)}
        with mock.patch.object(ssh_control, 'master_socket_exists', return_value=True):
            b._populate_hosts_list()
        self.assertIn("🔀 1", self._texts(b)["alpha"].text())

    def test_forget_on_master_drop(self):
        panel = self._panel()
        panel._active_forwards["alpha"] = {self.rew._ForwardsDialog.rule_key(_RULE)}
        with mock.patch.object(ssh_control, 'master_exit', return_value=True), \
             mock.patch.object(ssh_control, 'master_socket_exists', return_value=True):
            panel._drop_master(self.hosts[0])
        self.assertNotIn("alpha", panel._active_forwards)
        self.assertNotIn("🔀", self._texts(panel)["alpha"].text())


def self_role():
    import remote_explorer_widget as rew
    return rew._ROLE_ENTRY


if __name__ == '__main__':
    unittest.main()
