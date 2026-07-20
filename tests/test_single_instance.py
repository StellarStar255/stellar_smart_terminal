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
# 事件驱动：收到即退，消除固定定时在慢机（Windows CI 命名管道）上的竞态
def _on_dir(p):
    received.append(p)
    primary.quit()
primary._open_dir = _on_dir
assert primary.start_single_instance_server(), "listen failed"
name = app_mod._single_instance_server_name()
target = os.path.expanduser("~")

def forward():
    s = QLocalSocket()
    # 命名管道可能尚未就绪，重试几次连接
    for _ in range(20):
        s.connectToServer(name)
        if s.waitForConnected(500):
            break
        s.abort()
    else:
        return  # 连不上就让兜底定时退出，received 为空 → 断言失败可诊断
    s.write((target + "\n").encode()); s.waitForBytesWritten(2000); s.flush()
    s.waitForDisconnected(500)

QTimer.singleShot(100, forward)
QTimer.singleShot(8000, primary.quit)  # 兜底：正常路径早已由 _on_dir 退出
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
                              capture_output=True, text=True, timeout=120, env=env)
        self.assertIn("OK", proc.stdout,
                      msg=f"stdout={proc.stdout}\nstderr={proc.stderr}")


if __name__ == "__main__":
    unittest.main()
