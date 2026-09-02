"""软链安全：stat() 必须如实上报 is_link；remove_tree 绝不能穿透软链进入目标目录。

历史 bug：paramiko 后端 SSHSession.stat 用 sftp.stat（跟随软链）算 is_link，
"指向目录的软链"永远报 is_dir=True, is_link=False；面板据此调 remove_tree，
而 SFTP 服务端的 listdir 同样跟随软链——结果把链接**目标目录**里的东西
逐个删光，最后 rmdir(link) 失败才停下。ControlMaster 后端用 rm -rf 只删
链接本身，两后端行为分叉。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_ssh_symlink_remove -v
"""
import os
import stat as _stat
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])


class _Attr:
    """paramiko.SFTPAttributes 的最小替身"""

    def __init__(self, mode, size=0, mtime=0, filename=None):
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime
        self.filename = filename


class FakeSftp:
    """内存里的远端文件系统，行为对齐 OpenSSH sftp-server：

    - lstat 不跟随软链，stat / listdir_attr 跟随软链（listdir 一个指向目录的
      软链会列出目标目录的内容——这正是历史 bug 的成因）。
    - remove / rmdir 只记录调用，不真的改结构，方便断言"删了什么"。
    """

    def __init__(self, dirs, files, links):
        self.dirs = {d: list(children) for d, children in dirs.items()}
        self.files = set(files)
        self.links = dict(links)
        self.removed = []
        self.rmdired = []
        self.listed = []

    # -- helpers --
    def _resolve(self, path):
        seen = 0
        while path in self.links:
            path = self.links[path]
            seen += 1
            if seen > 16:
                raise IOError("too many links")
        return path

    def _mode_nofollow(self, path):
        if path in self.links:
            return _stat.S_IFLNK | 0o777
        if path in self.dirs:
            return _stat.S_IFDIR | 0o755
        if path in self.files:
            return _stat.S_IFREG | 0o644
        raise IOError(f"no such file: {path}")

    # -- sftp API --
    def lstat(self, path):
        return _Attr(self._mode_nofollow(path))

    def stat(self, path):
        return _Attr(self._mode_nofollow(self._resolve(path)), size=7)

    def listdir_attr(self, path):
        real = self._resolve(path)
        self.listed.append(path)
        if real not in self.dirs:
            raise IOError(f"not a directory: {path}")
        out = []
        for name in self.dirs[real]:
            child = real.rstrip("/") + "/" + name
            out.append(_Attr(self._mode_nofollow(child), filename=name))
        return out

    def remove(self, path):
        self.removed.append(path)

    def rmdir(self, path):
        self.rmdired.append(path)


def _fs():
    """/data/real 是真目录（含 a.txt、子目录 sub/、以及一个指向 /etc 的软链 cfg）
    /data/link -> /data/real
    /etc 里有 passwd —— 任何测试里它都不许被删"""
    return FakeSftp(
        dirs={
            "/data": ["real", "link"],
            "/data/real": ["a.txt", "sub", "cfg"],
            "/data/real/sub": ["b.txt"],
            "/etc": ["passwd"],
        },
        files={"/data/real/a.txt", "/data/real/sub/b.txt", "/etc/passwd"},
        links={"/data/link": "/data/real", "/data/real/cfg": "/etc"},
    )


class TestParamikoSymlinkSafety(_Base):
    def _session(self, fake):
        from ssh_session import SSHSession, HostConfig
        sess = SSHSession(HostConfig(alias="t", hostname="h"))
        self.addCleanup(sess._executor.shutdown, wait=False)
        sess._sftp = fake
        return sess

    def test_stat_on_symlink_to_dir_reports_is_link(self):
        fake = _fs()
        sess = self._session(fake)
        entry = sess.stat("/data/link")
        # 导航要跟随（is_dir=True），删除要认得链接身份（is_link=True）
        self.assertTrue(entry.is_dir)
        self.assertTrue(entry.is_link)

    def test_stat_on_real_dir_and_file(self):
        fake = _fs()
        sess = self._session(fake)
        d = sess.stat("/data/real")
        self.assertTrue(d.is_dir)
        self.assertFalse(d.is_link)
        f = sess.stat("/data/real/a.txt")
        self.assertFalse(f.is_dir)
        self.assertFalse(f.is_link)

    def test_stat_dangling_symlink_does_not_raise(self):
        fake = FakeSftp(dirs={"/d": ["gone"]}, files=set(), links={"/d/gone": "/nowhere"})
        sess = self._session(fake)
        entry = sess.stat("/d/gone")
        self.assertTrue(entry.is_link)
        self.assertFalse(entry.is_dir)

    def test_remove_tree_on_symlink_removes_only_the_link(self):
        fake = _fs()
        sess = self._session(fake)
        sess.remove_tree("/data/link")
        self.assertEqual(fake.removed, ["/data/link"])
        self.assertEqual(fake.rmdired, [])
        # 绝不能列出/删除目标目录里的任何东西
        self.assertEqual(fake.listed, [])
        self.assertNotIn("/data/real/a.txt", fake.removed)

    def test_remove_tree_on_real_dir_removes_symlinked_subdir_link_only(self):
        fake = _fs()
        sess = self._session(fake)
        sess.remove_tree("/data/real")
        # 真正的子目录被递归删除
        self.assertIn("/data/real/sub/b.txt", fake.removed)
        self.assertIn("/data/real/sub", fake.rmdired)
        self.assertIn("/data/real", fake.rmdired)
        # cfg -> /etc 只删链接，不进入 /etc
        self.assertIn("/data/real/cfg", fake.removed)
        self.assertNotIn("/etc/passwd", fake.removed)
        self.assertNotIn("/etc", fake.rmdired)
        self.assertNotIn("/data/real/cfg", fake.listed)

    def test_panel_delete_path_removes_link_not_target(self):
        """面板 _remote_remove 的决策：stat → is_dir and not is_link → remove_tree，
        否则 remove。对软链目录必须走 remove。"""
        fake = _fs()
        sess = self._session(fake)
        entry = sess.stat("/data/link")
        if entry.is_dir and not entry.is_link:
            sess.remove_tree("/data/link")
        else:
            sess.remove("/data/link")
        self.assertEqual(fake.removed, ["/data/link"])
        self.assertNotIn("/data/real/a.txt", fake.removed)


class TestControlMasterSymlinkParity(_Base):
    def _sess(self):
        import ssh_control
        from ssh_session import HostConfig
        return ssh_control.ControlMasterSession(HostConfig(alias="t", hostname="h"))

    def test_stat_reports_is_link_for_symlink_to_dir(self):
        sess = self._sess()
        seen = {}

        def _run(cmd, timeout=None):
            seen['cmd'] = cmd
            return "dir link\ndrwxr-xr-x 2 0 0 4096 1712345678 real\n"

        sess._run = _run
        st = sess.stat('/data/link')
        self.assertIn('-L', seen['cmd'])
        self.assertTrue(st.is_dir)
        self.assertTrue(st.is_link)

    def test_stat_plain_dir_is_not_link(self):
        sess = self._sess()
        sess._run = lambda cmd, timeout=None: "dir\ndrwxr-xr-x 2 0 0 4096 1712345678 real\n"
        st = sess.stat('/data/real')
        self.assertTrue(st.is_dir)
        self.assertFalse(st.is_link)

    def test_stat_dangling_symlink_does_not_raise(self):
        sess = self._sess()
        sess._run = lambda cmd, timeout=None: "none link\n"
        st = sess.stat('/data/gone')
        self.assertTrue(st.is_link)
        self.assertFalse(st.is_dir)

    def test_listdir_marks_symlinks(self):
        import ssh_control
        out = ssh_control.parse_ls_output(
            "lrwxrwxrwx 1 0 0 9 1712345678 link -> /data/real\n"
            "drwxr-xr-x 2 0 0 4096 1712345678 real\n", "/data")
        by_name = {e.name: e for e in out}
        self.assertTrue(by_name["link"].is_link)
        self.assertFalse(by_name["link"].is_dir)
        self.assertFalse(by_name["real"].is_link)


if __name__ == "__main__":
    unittest.main()
