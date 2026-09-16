"""两个 SSH 后端的 upload()/mkdir() 语义要一致（2026-09 审查 E 项）。

ControlMasterSession.upload 走 `.part` + mv（半成品不会被别人看见）、mkdir 是
`mkdir -p`；paramiko 的 SSHSession.upload 却直接 sftp.put 到目标（传一半断线
留半个文件）、mkdir 对已存在目录抛错。面板按同一套接口调两个后端。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_upload_parity_2026_09_ssh.py -q
"""
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication  # noqa: E402

from ssh_session import HostConfig, SSHSession  # noqa: E402


class _FakeSftp:
    def __init__(self, existing=None, put_fails=False):
        self.calls = []
        self.existing = dict(existing or {})     # path → st_mode
        self.put_fails = put_fails

    def put(self, local, remote, callback=None):
        self.calls.append(('put', local, remote))
        if self.put_fails:
            raise OSError('socket closed')

    def posix_rename(self, old, new):
        self.calls.append(('posix_rename', old, new))

    def rename(self, old, new):
        self.calls.append(('rename', old, new))

    def remove(self, path):
        self.calls.append(('remove', path))

    def stat(self, path):
        self.calls.append(('stat', path))
        if path not in self.existing:
            raise IOError(2, 'No such file')
        attr = type('A', (), {})()
        attr.st_mode = self.existing[path]
        return attr

    def mkdir(self, path):
        self.calls.append(('mkdir', path))
        if path in self.existing:
            raise IOError(17, 'File exists')
        self.existing[path] = stat.S_IFDIR | 0o755


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _sess(self, sftp):
        sess = SSHSession(HostConfig(alias='gpu', hostname='10.0.0.1'))
        sess._sftp = sftp
        self.addCleanup(sess._executor.shutdown, False)
        return sess


class UploadIsAtomic(_Base):

    def test_upload_puts_to_part_then_renames(self):
        sftp = _FakeSftp()
        tmp = tempfile.mkdtemp(prefix='up-')
        local = os.path.join(tmp, 'a.png')
        open(local, 'wb').close()
        self._sess(sftp).upload(local, '/data/.images/a.png')
        self.assertEqual(sftp.calls, [
            ('put', local, '/data/.images/a.png.part'),
            ('posix_rename', '/data/.images/a.png.part', '/data/.images/a.png'),
        ])

    def test_failed_upload_removes_the_part(self):
        sftp = _FakeSftp(put_fails=True)
        tmp = tempfile.mkdtemp(prefix='up-')
        local = os.path.join(tmp, 'a.png')
        open(local, 'wb').close()
        with self.assertRaises(OSError):
            self._sess(sftp).upload(local, '/data/a.png')
        self.assertIn(('remove', '/data/a.png.part'), sftp.calls)
        self.assertNotIn(('posix_rename', '/data/a.png.part', '/data/a.png'), sftp.calls)


class MkdirIsIdempotent(_Base):

    def test_existing_directory_is_not_an_error(self):
        sftp = _FakeSftp(existing={'/data/.images': stat.S_IFDIR | 0o755})
        self._sess(sftp).mkdir('/data/.images')        # 以前这里抛 IOError(17)
        self.assertNotIn(('mkdir', '/data/.images'), sftp.calls)

    def test_missing_directory_is_created(self):
        sftp = _FakeSftp()
        self._sess(sftp).mkdir('/data/.images')
        self.assertIn(('mkdir', '/data/.images'), sftp.calls)

    def test_existing_file_in_the_way_still_errors(self):
        """同名是个普通文件：不能假装建好了（与远端 mkdir -p 报 File exists 一致）。"""
        sftp = _FakeSftp(existing={'/data/.images': stat.S_IFREG | 0o644})
        with self.assertRaises(OSError):
            self._sess(sftp).mkdir('/data/.images')


if __name__ == '__main__':
    unittest.main()
