"""SSHSession.abort() / 通道超时配置 —— 网络切换时不再无限期卡死。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_ssh_abort -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])


class TestAbortSafety(_Base):
    def _session(self):
        from ssh_session import SSHSession, HostConfig
        cfg = HostConfig(alias="t", hostname="192.0.2.1", user="x", port=22)
        return SSHSession(cfg)

    def test_abort_before_connect_is_noop(self):
        sess = self._session()
        # 未连接时调用不应抛异常，且应判定为非存活
        sess.abort()
        self.assertFalse(sess.is_alive())

    def test_abort_disables_autoreconnect(self):
        # abort 要置 _was_connected=False，否则被取消的传输会被 _auto_reconnect 重试
        sess = self._session()
        sess._was_connected = True
        sess.abort()
        self.assertFalse(sess._was_connected)
        # _should_reconnect 在 was_connected=False 时必须返回 False（不重连）
        self.assertFalse(sess._should_reconnect(OSError("boom")))

    def test_has_abort_method(self):
        sess = self._session()
        self.assertTrue(callable(getattr(sess, "abort", None)))


class TestTimeoutConfigured(_Base):
    def test_sftp_op_timeout_constant(self):
        import ssh_session
        # 必须存在且为正数（网络断流时 recv 的读超时上限）
        self.assertTrue(hasattr(ssh_session, "SFTP_OP_TIMEOUT"))
        self.assertGreater(ssh_session.SFTP_OP_TIMEOUT, 0)


if __name__ == "__main__":
    unittest.main()
