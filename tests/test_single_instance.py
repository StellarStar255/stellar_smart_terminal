"""单实例 IPC 守卫（Linux/Windows 右键菜单并入现有实例，不起孤立进程）

server name 的用户 + 数据目录隔离是关键逻辑；IPC 往返用子进程做真测试，
避免与 suite 里已存在的 QApplication 单例冲突。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_mod

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestServerName(unittest.TestCase):
    def test_stable_and_has_uid(self):
        n1 = app_mod._single_instance_server_name()
        n2 = app_mod._single_instance_server_name()
        self.assertEqual(n1, n2)
        self.assertTrue(n1.startswith("stellar-smart-terminal-"))

    def test_different_data_dir_different_name(self):
        import utils
        orig = utils.get_data_dir
        try:
            utils.get_data_dir = lambda: Path("/tmp/aaa")
            a = app_mod._single_instance_server_name()
            utils.get_data_dir = lambda: Path("/tmp/bbb")
            b = app_mod._single_instance_server_name()
        finally:
            utils.get_data_dir = orig
        self.assertNotEqual(a, b)  # 不同数据目录 → 不同实例，不会误并入


_HARNESS = r'''
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, %r)
from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QLocalSocket
import app as app_mod

primary = app_mod.SmartTerminalApplication([])
received = []
primary._open_dir = lambda p: received.append(p)
assert primary.start_single_instance_server(), "listen failed"
name = app_mod._single_instance_server_name()
target = os.path.expanduser("~")

def forward():
    s = QLocalSocket()
    s.connectToServer(name)
    assert s.waitForConnected(500), "connect failed"
    s.write((target + "\n").encode()); s.waitForBytesWritten(1000); s.flush()
    s.waitForDisconnected(300)

QTimer.singleShot(100, forward)
QTimer.singleShot(700, primary.quit)
primary.exec()
assert received == [target], "primary did not receive: %%r" %% received
# 无主实例时转发返回 False
from PyQt6.QtNetwork import QLocalServer
QLocalServer.removeServer(name)
assert primary.try_forward_to_primary("/tmp") is False, "should be False w/o primary"
print("OK")
''' % REPO


class TestIpcRoundtrip(unittest.TestCase):
    def test_primary_receives_forwarded_dir(self):
        env = dict(os.environ)
        # 隔离数据目录，别碰真实配置
        import tempfile
        env["STELLAR_DATA_DIR"] = tempfile.mkdtemp(prefix="si_test_")
        plugin = ("/opt/anaconda3/lib/python3.13/site-packages/PyQt6/"
                  "Qt6/plugins/platforms")
        if os.path.isdir(plugin):
            env.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", plugin)
        proc = subprocess.run([sys.executable, "-c", _HARNESS],
                              capture_output=True, text=True, timeout=60, env=env)
        self.assertIn("OK", proc.stdout,
                      msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")


if __name__ == "__main__":
    unittest.main()
