"""常驻主连接必须按「交互会话」档打 DSCP 标记，否则挂在上面的端口转发比手敲 ssh 慢。

OpenSSH 的 IPQoS 默认分交互 / 非交互两档（新版 ef / cs0，旧版 af21 / cs1），
连接建立时设一次就锁定；`-M -N` 的主连接被判成非交互 → 最低档。用户手敲
`ssh -L` 带 shell 是交互 → 高档。应用建主连接时显式按 `ssh -G` 报告的交互档
标记，与用户本机 OpenSSH 版本 / ~/.ssh/config 一致。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_master_ipqos.py -v
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssh_control  # noqa: E402
from ssh_session import HostConfig  # noqa: E402


def _proc(stdout=b"", rc=0):
    m = mock.Mock()
    m.returncode = rc
    m.stdout = stdout
    m.stderr = b""
    return m


class MasterIpqosTest(unittest.TestCase):

    def setUp(self):
        ssh_control._IPQOS_CACHE.clear()
        self.addCleanup(ssh_control._IPQOS_CACHE.clear)
        self.host = HostConfig(alias='gpu-main1', hostname='10.10.80.70',
                               user='huangqiliang', port=2222)

    def _opt(self, args, key):
        for i, a in enumerate(args):
            if a == '-o' and args[i + 1].startswith(key + '='):
                return args[i + 1].split('=', 1)[1]
        return None

    def test_master_args_carry_interactive_class_from_ssh_G(self):
        """新版 OpenSSH：ssh -G 报 `ipqos ef cs0` → 主连接用 ef。"""
        with mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(b"user huangqiliang\nipqos ef cs0\nport 2222\n")) as run:
            args = ssh_control._master_args(self.host, '/tmp/ctl', 'yes', batch=True)
        self.assertEqual(self._opt(args, 'IPQoS'), 'ef')
        self.assertEqual(run.call_args[0][0][:2], ['ssh', '-G'])

    def test_old_openssh_default_af21(self):
        """旧版 OpenSSH：`ipqos af21 cs1` → 主连接用 af21。"""
        with mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(b"ipqos af21 cs1\n")):
            args = ssh_control._master_args(self.host, '/tmp/ctl', 'yes', batch=True)
        self.assertEqual(self._opt(args, 'IPQoS'), 'af21')

    def test_ssh_G_failure_falls_back_to_ef_and_is_not_cached(self):
        """ssh -G 失败 → 回退 ef（现代交互档），而不是把主连接标成低优先级 af21；
        且回退值不缓存——一次瞬时失败不能钉死整个进程后续所有主连接的档。"""
        with mock.patch.object(ssh_control.subprocess, 'run',
                               side_effect=OSError("no ssh")) as run:
            a1 = ssh_control._master_args(self.host, '/tmp/ctl', 'yes', batch=True)
            a2 = ssh_control._master_args(self.host, '/tmp/ctl', 'yes', batch=False)
        self.assertEqual(self._opt(a1, 'IPQoS'), 'ef')
        self.assertEqual(self._opt(a2, 'IPQoS'), 'ef')
        self.assertEqual(run.call_count, 2, "回退值不该缓存，第二次要再试 ssh -G")

    def test_ssh_G_success_is_cached(self):
        with mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(b"ipqos ef cs0\n")) as run:
            ssh_control.interactive_ipqos(self.host)
            ssh_control.interactive_ipqos(self.host)
        self.assertEqual(run.call_count, 1, "成功读到的值才按目标缓存")

    def test_real_ssh_G_on_this_machine_yields_interactive_class(self):
        """真跑一次 ssh -G（不联网）：取到的必须是交互档，不是 none/cs0/cs1。"""
        if not ssh_control.is_supported():
            self.skipTest("no OpenSSH ControlMaster on this platform")
        val = ssh_control.interactive_ipqos(self.host)
        self.assertNotIn(val, ('none', 'cs0', 'cs1', 'le'), val)


if __name__ == '__main__':
    unittest.main()
