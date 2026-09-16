"""陈旧 control socket 不能让「建主连接」假成功并泄漏 `ssh -N -f` 孤儿（2026-09 审查 A 项）。

OpenSSH 的 muxserver_listen 遇到 ControlPath 已存在会打印
`ControlSocket ... already exists, disabling multiplexing` 然后**继续普通连接
并返回 0**；配上 -N -f 就是一条永生的后台 ssh，既不是主连接、也没人会收拾。
模块以前从不 unlink 陈旧 socket，用户机器上就会越攒越多。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_ssh_control_stale_socket_2026_09_ssh.py -q
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssh_control  # noqa: E402
from ssh_session import HostConfig  # noqa: E402

_DEGRADED = (b"ControlSocket /tmp/stellar-ssh/c-abc already exists, "
             b"disabling multiplexing\n")


def _proc(rc=0, out=b"", err=b""):
    m = mock.Mock()
    m.returncode = rc
    m.stdout = out
    m.stderr = err
    return m


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='stale-')
        self.ctl = os.path.join(self.tmp, 'c-abc')
        self.host = HostConfig(alias='gpu', hostname='10.0.0.1', user='u', port=22)
        ssh_control._IPQOS_CACHE.clear()
        self.addCleanup(ssh_control._IPQOS_CACHE.clear)
        # Windows 没 ControlMaster：只测决策逻辑，平台判断桩成支持
        for name, val in (('is_supported', True), ('control_path_for', self.ctl)):
            p = mock.patch.object(ssh_control, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    def _touch_stale(self):
        open(self.ctl, 'w').close()
        with open(self.ctl + '.qos', 'w') as fh:
            fh.write('af21')


class StaleSocketClearedBeforeConnect(_Base):
    """socket 文件在、`-O check` 不通 → 建连**之前**先 unlink（含 .qos 记账）。"""

    def _run_recording(self, seen):
        def run(argv, **kw):
            if argv[:2] == ['ssh', '-G']:
                return _proc(0, b"ipqos ef cs0\n")
            if '-N' in argv:
                seen['socket_at_connect'] = os.path.exists(self.ctl)
                seen['qos_at_connect'] = os.path.exists(self.ctl + '.qos')
                return _proc(0)
            return _proc(255)
        return run

    def test_keys_path_unlinks_stale_socket_first(self):
        self._touch_stale()
        seen = {}
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=self._run_recording(seen)):
            ssh_control.start_master_with_keys(self.host)
        self.assertIn('socket_at_connect', seen, "应该跑到 ssh -N -f")
        self.assertFalse(seen['socket_at_connect'], "陈旧 socket 必须在建连前被 unlink")
        self.assertFalse(seen['qos_at_connect'], ".qos 记账要一起清掉")

    def test_mfa_login_unlinks_stale_socket_first(self):
        self._touch_stale()
        seen = {}
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=self._run_recording(seen)):
            ssh_control.mfa_login(self.host, code='123456')
        self.assertFalse(seen.get('socket_at_connect', True))

    def test_ensure_master_unlinks_stale_socket_first(self):
        self._touch_stale()
        seen = {}
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=self._run_recording(seen)):
            ssh_control.ensure_master(self.host)
        self.assertFalse(seen.get('socket_at_connect', True))

    def test_refresh_master_clears_socket_that_survived_exit(self):
        """`-O exit` 后 socket 仍在（主连接早死了，exit 根本没人应）→ 也要清掉。"""
        self._touch_stale()
        seen = {}
        with mock.patch.object(ssh_control, 'master_exit', return_value=False), \
             mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.time, 'sleep'), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=self._run_recording(seen)):
            ssh_control.refresh_master(self.host)
        self.assertFalse(seen.get('socket_at_connect', True))

    def test_live_master_socket_is_left_alone(self):
        """活着的主连接的 socket 绝不能碰。"""
        self._touch_stale()
        with mock.patch.object(ssh_control, 'master_alive', return_value=True):
            self.assertFalse(ssh_control._clear_stale_socket(self.host, self.ctl))
        self.assertTrue(os.path.exists(self.ctl))


class DegradedMasterIsRejected(_Base):
    """ssh 返回 0 但 stderr 说 disabling multiplexing → 不是主连接，视为失败：
    杀掉刚起的后台 ssh、不写 .qos、抛明确错误。"""

    def _run(self, argv, **kw):
        if argv[:2] == ['ssh', '-G']:
            return _proc(0, b"ipqos ef cs0\n")
        if '-N' in argv:
            return _proc(0, b"", _DEGRADED)
        if argv[:3] == ['ssh', '-O', 'check']:
            return _proc(255, b"", b"Control socket connect: No such file or directory\n")
        return _proc(0)

    def _orphans(self):
        return [(4242, f"ssh -o BatchMode=yes -o ControlMaster=yes -o ControlPath={self.ctl} "
                       f"-o ControlPersist=8h -N -f u@10.0.0.1")]

    def test_keys_path_raises_and_kills_orphan(self):
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=self._run), \
             mock.patch.object(ssh_control, '_processes_with_control_path', create=True,
                               return_value=self._orphans()), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            with self.assertRaises(RuntimeError) as cm:
                ssh_control.start_master_with_keys(self.host)
        self.assertIn('multiplexing', str(cm.exception).lower())
        self.assertFalse(os.path.exists(self.ctl + '.qos'), "假成功不能写 .qos 记账")
        kill.assert_called_once_with(4242)

    def test_mfa_login_raises_and_kills_orphan(self):
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=self._run), \
             mock.patch.object(ssh_control, '_processes_with_control_path', create=True,
                               return_value=self._orphans()), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            with self.assertRaises(RuntimeError):
                ssh_control.mfa_login(self.host, code='123456')
        self.assertFalse(os.path.exists(self.ctl + '.qos'))
        kill.assert_called_once_with(4242)

    def test_live_master_is_never_the_one_killed(self):
        """退化连接与真主连接并存（并发建连撞上）：只杀 -O check 报的 pid 以外的。"""
        def run(argv, **kw):
            if argv[:3] == ['ssh', '-O', 'check']:
                return _proc(0, b"", b"Master running (pid=1111)\n")
            return self._run(argv, **kw)

        procs = self._orphans() + [(1111, f"ssh -o ControlMaster=yes -o ControlPath={self.ctl} -N -f u@10.0.0.1")]
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control, 'master_pid', return_value=1111, create=True), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=run), \
             mock.patch.object(ssh_control, '_processes_with_control_path', return_value=procs, create=True), \
             mock.patch.object(ssh_control, '_kill_wait') as kill:
            with self.assertRaises(RuntimeError):
                ssh_control.start_master_with_keys(self.host)
        self.assertEqual([c.args[0] for c in kill.call_args_list], [4242])

    def test_clean_success_still_writes_qos(self):
        """对照：stderr 干净的成功照旧记账。"""
        def run(argv, **kw):
            if argv[:2] == ['ssh', '-G']:
                return _proc(0, b"ipqos ef cs0\n")
            return _proc(0)
        with mock.patch.object(ssh_control, 'master_alive', return_value=False), \
             mock.patch.object(ssh_control.subprocess, 'run', side_effect=run):
            ssh_control.start_master_with_keys(self.host)
        with open(self.ctl + '.qos') as fh:
            self.assertEqual(fh.read().strip(), 'ef')


class ProcessHelpers(unittest.TestCase):

    def test_master_pid_parsed_from_check_output(self):
        host = HostConfig(alias='gpu', hostname='10.0.0.1', user='u', port=22)
        with mock.patch.object(ssh_control, 'is_supported', return_value=True), \
             mock.patch.object(ssh_control, 'control_path_for', return_value='/tmp/c-x'), \
             mock.patch.object(ssh_control.os.path, 'exists', return_value=True), \
             mock.patch.object(ssh_control.subprocess, 'run',
                               return_value=_proc(0, b"", b"Master running (pid=31337)\n")):
            self.assertEqual(ssh_control.master_pid(host), 31337)
        with mock.patch.object(ssh_control, 'is_supported', return_value=True), \
             mock.patch.object(ssh_control, 'control_path_for', return_value='/tmp/c-x'), \
             mock.patch.object(ssh_control.os.path, 'exists', return_value=True), \
             mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(255)):
            self.assertEqual(ssh_control.master_pid(host), 0)

    def test_processes_with_control_path_filters_ps_rows(self):
        ps = (b"  100 ssh -o ControlMaster=yes -o ControlPath=/tmp/c-x -N -f gpu\n"
              b"  101 ssh -o ControlMaster=no -o ControlPath=/tmp/c-x -o ServerAliveInterval=15 gpu\n"
              b"  102 ssh -o ControlPath=/tmp/c-other gpu\n"
              b"  103 vim ssh_control.py\n")
        with mock.patch.object(ssh_control.subprocess, 'run', return_value=_proc(0, ps)):
            rows = ssh_control._processes_with_control_path('/tmp/c-x')
        self.assertEqual([pid for pid, _ in rows], [100, 101])


if __name__ == '__main__':
    unittest.main()
