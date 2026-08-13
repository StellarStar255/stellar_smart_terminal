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


class TestOpenSftpBounded(_Base):
    """打开 SFTP 通道必须有整体超时。

    paramiko 的 open_sftp() 内部 invoke_subsystem 用 Channel._wait_for_event()，
    那个等待没有超时——服务器 TCP/认证都通但 sftp 子系统不响应时会永久卡住
    会话唯一的 worker，连锁冻住整个远程面板。
    """

    def _session(self):
        from ssh_session import SSHSession, HostConfig
        return SSHSession(HostConfig(alias="t", hostname="192.0.2.1"))

    def test_hanging_open_sftp_times_out_and_closes_transport(self):
        import threading
        import ssh_session

        sess = self._session()
        self.addCleanup(sess._executor.shutdown, wait=False)
        closed = threading.Event()
        release = threading.Event()

        class _Transport:
            def close(self):
                closed.set()
                release.set()      # 让卡住的 open_sftp 得以退出

        class _HangingClient:
            def open_sftp(self):
                release.wait(10)   # 模拟永不响应的 sftp 子系统
                raise OSError("transport closed")

            def get_transport(self):
                return _Transport()

        orig = ssh_session.SFTP_OPEN_TIMEOUT
        ssh_session.SFTP_OPEN_TIMEOUT = 0.3      # 缩短以便快速测试
        try:
            with self.assertRaises(Exception) as ctx:
                sess._open_sftp_bounded(_HangingClient())
        finally:
            ssh_session.SFTP_OPEN_TIMEOUT = orig
            release.set()
        self.assertIn("did not respond", str(ctx.exception))
        self.assertTrue(closed.is_set(),
                        "超时后必须关掉 transport，否则卡住的线程永不退出")

    def test_normal_open_returns_sftp_with_timeout_set(self):
        sess = self._session()
        self.addCleanup(sess._executor.shutdown, wait=False)
        import ssh_session

        applied = {}

        class _Chan:
            def settimeout(self, v):
                applied['timeout'] = v

        class _Sftp:
            def get_channel(self):
                return _Chan()

        class _OkClient:
            def open_sftp(self):
                return _Sftp()

            def get_transport(self):
                raise AssertionError("正常路径不该关 transport")

        sftp = sess._open_sftp_bounded(_OkClient())
        self.assertIsInstance(sftp, _Sftp)
        self.assertEqual(applied.get('timeout'), ssh_session.SFTP_OP_TIMEOUT)


class TestReconnectCooldown(_Base):
    """重连冷却：断线时排队的 N 个操作不应各自跑一轮完整重连。

    没有冷却时，一次目录粘贴排队的几百个 upload 会每个都重连 3 次
    （最坏数十秒），总时长 N 倍 —— 表现为「一个远程操作挂了，面板长时间
    毫无反应」。
    """

    def _session(self):
        from ssh_session import SSHSession, HostConfig
        s = SSHSession(HostConfig(alias="t", hostname="192.0.2.1"))
        self.addCleanup(s._executor.shutdown, wait=False)
        return s

    def test_only_first_operation_pays_reconnect_cost(self):
        sess = self._session()
        attempts = []

        def _fake_reconnect():
            attempts.append(1)
            raise RuntimeError("unreachable")
        sess._reconnect = _fake_reconnect

        for _ in range(20):
            with self.assertRaises(RuntimeError):
                sess._reconnect_or_fail_fast()
        self.assertEqual(len(attempts), 1,
                         f"冷却期内只应尝试一次重连，实际 {len(attempts)} 次")

    def test_cooldown_expires_and_allows_retry(self):
        import time
        import ssh_session

        sess = self._session()
        attempts = []

        def _fake_reconnect():
            attempts.append(1)
            raise RuntimeError("unreachable")
        sess._reconnect = _fake_reconnect

        orig = ssh_session.RECONNECT_COOLDOWN
        ssh_session.RECONNECT_COOLDOWN = 0.15
        try:
            with self.assertRaises(RuntimeError):
                sess._reconnect_or_fail_fast()
            time.sleep(0.2)                      # 冷却期过
            with self.assertRaises(RuntimeError):
                sess._reconnect_or_fail_fast()
        finally:
            ssh_session.RECONNECT_COOLDOWN = orig
        self.assertEqual(len(attempts), 2,
                         "冷却期结束后应允许再次尝试重连（网络恢复要能自愈）")

    def test_success_clears_cooldown(self):
        sess = self._session()
        sess._last_reconnect_failure = None
        sess._reconnect = lambda: None           # 重连成功
        sess._reconnect_or_fail_fast()
        self.assertIsNone(sess._last_reconnect_failure)


class TestDisconnectIsNonBlocking(_Base):
    """disconnect() 必须瞬时返回：旧实现把清理排到单 worker 队列末尾再
    等 5 秒，前面有在跑的传输时必然等满 —— GUI 硬冻 5s 且清理还没做成。"""

    def test_disconnect_does_not_wait_for_busy_worker(self):
        import threading
        import time
        from ssh_session import SSHSession, HostConfig

        sess = SSHSession(HostConfig(alias="t", hostname="192.0.2.1"))
        blocking = threading.Event()
        # 占住单 worker，模拟「正在跑一个卡住的传输」
        sess._executor.submit(lambda: blocking.wait(30))
        time.sleep(0.05)
        try:
            t0 = time.monotonic()
            sess.disconnect()
            elapsed = time.monotonic() - t0
        finally:
            blocking.set()
        self.assertLess(elapsed, 1.0,
                        f"disconnect 阻塞了 {elapsed:.1f}s，应瞬时返回")
        self.assertIsNone(sess._sftp)
        self.assertIsNone(sess._client)


if __name__ == "__main__":
    unittest.main()
