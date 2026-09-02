# -*- coding: utf-8 -*-
"""known_hosts 容错加载的回归测试。

paramiko 5 的 `HostKeys.load()` 遇到真实的 `@cert-authority` / `@revoked`
行会因字段错位抛 InvalidHostKey（不是被跳过的 SSHException），于是
`_connect_client` 把整份 known_hosts 当"损坏"，走「主机密钥变更拦截已降级」
分支——用户主力机走 OpenSSH + CA，known_hosts 里几乎必然有这种行，
等于 paramiko 后端永远在降级模式下跑。

修复：逐行解析，注释/空行/`@` 标记行/未知密钥类型/坏行各自跳过，
其余条目照常加载（含 `|1|` 哈希行）；只有文件本身读不了才算降级。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_ssh_known_hosts_loader.py -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import paramiko
from paramiko.hostkeys import HostKeys

ED25519_A = ("ssh-ed25519 "
             "AAAAC3NzaC1lZDI1NTE5AAAAIPB3PnnK5wG3fXYZ0uOg9c7TPUPpTx0i6uwaRfPLI2Ye")


def _write_mixed_known_hosts(path: str) -> None:
    hashed = HostKeys.hash_host("host2.example")
    lines = [
        "# a comment line",
        "",
        f"@cert-authority *.example.com {ED25519_A}",
        f"@revoked host9 {ED25519_A}",
        "weird-keytype host3 AAAA",
        f"host1.example {ED25519_A}",
        f"{hashed} {ED25519_A}",
        "garbage line without enough fields",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


class TestTolerantLoader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kh_")
        self.path = os.path.join(self.tmp, "known_hosts")
        _write_mixed_known_hosts(self.path)

    def test_paramiko_itself_rejects_the_file(self):
        """前提确认：paramiko 原生加载在这份文件上就是抛异常的"""
        with self.assertRaises(Exception):
            HostKeys(self.path)

    def test_loader_keeps_valid_entries_and_skips_the_rest(self):
        from ssh_session import load_known_hosts_tolerant
        client = paramiko.SSHClient()
        loaded, skipped = load_known_hosts_tolerant(client, self.path)
        self.assertEqual(loaded, 2, "应加载 host1 与哈希的 host2 两条")
        self.assertEqual(skipped, 4, "@cert-authority/@revoked/未知类型/坏行各跳过一次")
        hk = client.get_host_keys()
        self.assertIsNotNone(hk.lookup("host1.example"))
        self.assertIsNotNone(hk.lookup("host2.example"), "哈希行必须仍可命中")
        self.assertIsNone(hk.lookup("host9"))


class TestConnectPathNotDegraded(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_cert_authority_line_does_not_trigger_degraded_branch(self):
        import ssh_session
        from ssh_session import SSHSession, HostConfig

        tmp = tempfile.mkdtemp(prefix="kh_")
        path = os.path.join(tmp, "known_hosts")
        _write_mixed_known_hosts(path)

        host = HostConfig(alias='h', hostname='localhost', user='u', port=22)
        sess = SSHSession(host)
        signals = []
        sess.host_key_check_degraded.connect(lambda msg: signals.append(msg))

        real_client_cls = paramiko.SSHClient

        class _StopAtConnect(real_client_cls):
            def connect(self, **kw):
                raise RuntimeError("stop here")  # 只测到加载阶段

        with mock.patch.object(ssh_session.paramiko, 'SSHClient', _StopAtConnect), \
             mock.patch.object(ssh_session.os.path, 'expanduser',
                               lambda p: path if p.endswith("known_hosts") else p):
            with self.assertRaises(Exception):
                sess._connect_client(host)

        self.app.processEvents()
        self.assertFalse(sess._host_key_degraded,
                         "含 @cert-authority 的 known_hosts 不该触发降级")
        self.assertEqual(signals, [])


if __name__ == '__main__':
    unittest.main()
