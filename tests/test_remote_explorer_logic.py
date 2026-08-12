"""remote_explorer 纯逻辑单元测试（不依赖真实 SSH）：

- _TransferRateTracker：滑动窗口速率 / 全程平均 ETA / 按 key 重置
- _fmt_size / _fmt_rate / _fmt_eta 格式化边界
- _looks_like_disconnect 断线文案判定（驱动一键重连提示）
- _sorted_entries / _visible_entries：目录置顶 + 各排序键 + 隐藏文件过滤
- _entries_fingerprint：自动刷新的变更指纹
- _unique_new_name：新建文件/文件夹重名递增
- _temp_local_path_for：别名净化 + 远端路径映射到稳定 temp 目录
- _upload_local_dir：本地目录递归上传的 os.sep→posix 路径映射（假 session）
- set_sort：非法排序键回退 name + 持久化

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_remote_explorer_logic -v
"""
import os
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_entry(name, is_dir=False, size=0, mtime=0.0):
    from ssh_session import RemoteEntry
    return RemoteEntry(name=name, path="/x/" + name, is_dir=is_dir,
                       size=size, mtime=mtime)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 配置读写经 app_config 单点；重定向到临时文件避免污染真实配置
        import app_config
        self._tmp_cfg = Path(tempfile.mkdtemp()) / "cfg.json"
        self._orig_get_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._tmp_cfg

    def tearDown(self):
        import app_config
        app_config.get_config_path = self._orig_get_path

    def _panel(self):
        from remote_explorer_widget import RemoteExplorerPanel
        return RemoteExplorerPanel(theme={})


class TestTransferRateTracker(_Base):
    """时间用假时钟推进，速率/ETA 可精确断言。"""

    def setUp(self):
        super().setUp()
        self._now = 0.0
        self._clock = mock.patch('time.monotonic', lambda: self._now)
        self._clock.start()

    def tearDown(self):
        self._clock.stop()
        super().tearDown()

    def _tracker(self):
        from remote_explorer_widget import _TransferRateTracker
        return _TransferRateTracker()

    def test_rate_needs_two_samples(self):
        tr = self._tracker()
        self.assertEqual(tr.rate(), 0.0)
        tr.update("f", 100)
        self.assertEqual(tr.rate(), 0.0)

    def test_rate_between_samples(self):
        tr = self._tracker()
        tr.update("f", 0)
        self._now = 1.0
        tr.update("f", 1000)
        self.assertAlmostEqual(tr.rate(), 1000.0)

    def test_window_trims_stale_samples(self):
        tr = self._tracker()
        # 前 2 秒没动，随后每秒 1000B：全程平均是 500B/s，
        # 但滑动窗口（2s）只看最近的加速段 → 1000B/s
        for ts, b in [(0, 0), (1, 0), (2, 0), (3, 1000), (4, 2000)]:
            self._now = float(ts)
            tr.update("f", b)
        self.assertAlmostEqual(tr.rate(), 1000.0)

    def test_key_change_resets(self):
        tr = self._tracker()
        tr.update("a", 5000)
        self._now = 1.0
        tr.update("b", 0)   # 换文件 → 窗口清空重来
        self.assertEqual(tr.rate(), 0.0)

    def test_eta_uses_overall_average(self):
        tr = self._tracker()
        tr.update("f", 0)
        self._now = 2.0
        tr.update("f", 1000)   # 平均 500B/s
        self.assertAlmostEqual(tr.eta_secs(1000, 3000), 4.0)

    def test_eta_none_when_unknown_or_done(self):
        tr = self._tracker()
        self.assertIsNone(tr.eta_secs(0, 100))          # 无采样
        tr.update("f", 50)
        self.assertIsNone(tr.eta_secs(50, 100))         # 时间未推进
        self.assertIsNone(tr.eta_secs(100, 100))        # 已完成
        self.assertIsNone(tr.eta_secs(50, 0))           # 总量未知


class TestFormatters(_Base):
    def _cls(self):
        from remote_explorer_widget import RemoteExplorerPanel
        return RemoteExplorerPanel

    def test_fmt_size_boundaries(self):
        P = self._cls()
        self.assertEqual(P._fmt_size(None), "?")
        self.assertEqual(P._fmt_size(-1), "?")
        self.assertEqual(P._fmt_size(0), "0 B")
        self.assertEqual(P._fmt_size(1023), "1023 B")
        self.assertEqual(P._fmt_size(1024), "1.0 KB")
        self.assertEqual(P._fmt_size(1536), "1.5 KB")
        self.assertEqual(P._fmt_size(1024 * 1024), "1.0 MB")
        self.assertEqual(P._fmt_size(1024 ** 3), "1.00 GB")

    def test_fmt_rate(self):
        self.assertEqual(self._cls()._fmt_rate(1536.9), "1.5 KB/s")

    def test_fmt_eta_rounds_to_nearest_second(self):
        P = self._cls()
        self.assertEqual(P._fmt_eta(0), "0:00")
        self.assertEqual(P._fmt_eta(3.4), "0:03")
        self.assertEqual(P._fmt_eta(59.6), "1:00")
        self.assertEqual(P._fmt_eta(125), "2:05")


class TestDisconnectHeuristic(_Base):
    def test_disconnect_messages(self):
        p = self._panel()
        for msg in ("Socket is closed",
                    "Connection reset by peer",
                    "[Errno 32] Broken pipe",
                    "EOF during negotiation",
                    "Server connection dropped: ",
                    "SSH session not active / channel closed"):
            self.assertTrue(p._looks_like_disconnect(msg), msg)

    def test_non_disconnect_messages(self):
        p = self._panel()
        for msg in ("", "Permission denied",
                    "No such file or directory: /a/b"):
            self.assertFalse(p._looks_like_disconnect(msg), repr(msg))


class TestEntriesSortFilter(_Base):
    def _entries(self):
        return [
            _make_entry("zeta.txt", size=10, mtime=3.0),
            _make_entry("Alpha.md", size=30, mtime=1.0),
            _make_entry("beta", is_dir=True, mtime=2.0),
            _make_entry("Sub", is_dir=True, mtime=5.0),
        ]

    def test_dirs_always_first(self):
        p = self._panel()
        for key in ("name", "size", "modified", "type"):
            for desc in (False, True):
                p._sort_key, p._sort_desc = key, desc
                out = p._sorted_entries(self._entries())
                self.assertEqual([e.is_dir for e in out],
                                 [True, True, False, False],
                                 f"key={key} desc={desc}")

    def test_name_sort_case_insensitive(self):
        p = self._panel()
        p._sort_key, p._sort_desc = "name", False
        out = p._sorted_entries(self._entries())
        self.assertEqual([e.name for e in out],
                         ["beta", "Sub", "Alpha.md", "zeta.txt"])
        p._sort_desc = True
        out = p._sorted_entries(self._entries())
        self.assertEqual([e.name for e in out],
                         ["Sub", "beta", "zeta.txt", "Alpha.md"])

    def test_size_sort_desc(self):
        p = self._panel()
        p._sort_key, p._sort_desc = "size", True
        out = p._sorted_entries(self._entries())
        self.assertEqual([e.name for e in out if not e.is_dir],
                         ["Alpha.md", "zeta.txt"])

    def test_type_sort_by_extension(self):
        p = self._panel()
        p._sort_key, p._sort_desc = "type", False
        out = p._sorted_entries(
            [_make_entry("b.txt"), _make_entry("a.zip"), _make_entry("c.md")])
        self.assertEqual([e.name for e in out], ["c.md", "b.txt", "a.zip"])

    def test_visible_entries_hides_dotfiles(self):
        p = self._panel()
        entries = [_make_entry(".git", is_dir=True),
                   _make_entry(".env"), _make_entry("app.py")]
        p._show_hidden = False
        self.assertEqual([e.name for e in p._visible_entries(entries)],
                         ["app.py"])
        p._show_hidden = True
        self.assertEqual(len(p._visible_entries(entries)), 3)


class TestEntriesFingerprint(_Base):
    def test_ignores_order_size_mtime(self):
        from remote_explorer_widget import RemoteExplorerPanel as P
        a = [_make_entry("a", size=1, mtime=1.0), _make_entry("d", is_dir=True)]
        b = [_make_entry("d", is_dir=True), _make_entry("a", size=99, mtime=9.0)]
        self.assertEqual(P._entries_fingerprint(a), P._entries_fingerprint(b))

    def test_detects_rename_and_type_change(self):
        from remote_explorer_widget import RemoteExplorerPanel as P
        base = [_make_entry("a")]
        self.assertNotEqual(P._entries_fingerprint(base),
                            P._entries_fingerprint([_make_entry("b")]))
        self.assertNotEqual(P._entries_fingerprint(base),
                            P._entries_fingerprint([_make_entry("a", is_dir=True)]))


class TestUniqueNewName(_Base):
    def test_increments_until_free(self):
        p = self._panel()
        for name in ("untitled.txt", "untitled2.txt", "keep"):
            item = p._make_item(_make_entry(name))
            p._tree.addTopLevelItem(item)
        self.assertEqual(p._unique_new_name(None, "untitled", ".txt"),
                         "untitled3.txt")
        self.assertEqual(p._unique_new_name(None, "fresh", ".txt"),
                         "fresh.txt")


class TestTempLocalPath(_Base):
    def test_alias_sanitized_and_remote_dirs_mirrored(self):
        from remote_explorer_widget import RemoteExplorerPanel
        path = RemoteExplorerPanel._temp_local_path_for(
            None, "my host:1", "/var/log/app.log", "app.log")
        expected_tail = os.path.join(
            "smart_terminal_remote_my_host_1", "var", "log", "app.log")
        self.assertTrue(path.endswith(expected_tail), path)
        self.assertTrue(path.startswith(tempfile.gettempdir()))
        self.assertTrue(os.path.isdir(os.path.dirname(path)))


class _FakeSession:
    """submit 即刻完成的假 SSH 会话：只记录提交了什么，不做任何 IO。

    remote_has_tar 返回 False → _upload_local_dir 走逐文件 SFTP 兜底路径。
    """

    def __init__(self):
        self.calls = []          # (fn_name, args)

    def submit(self, fn, *args):
        self.calls.append((fn.__name__, args))
        fut = Future()
        # remote_has_tar 的探测结果要真实返回，其余调用只记录不执行
        fut.set_result(fn(*args) if fn.__name__ == "remote_has_tar" else None)
        return fut

    def remote_has_tar(self):
        return False

    def mkdir(self, path):
        pass

    def upload_with_progress(self, local_path, remote_path, cb):
        pass

    def upload_dir_tar(self, local_dir, remote_dir, total_bytes=0,
                       progress_cb=None):
        pass

    def abort(self):
        pass


class TestUploadPathMapping(_Base):
    def test_local_tree_maps_to_posix_remote_paths(self):
        p = self._panel()
        src = Path(tempfile.mkdtemp())
        (src / "a.txt").write_text("aa", encoding="utf-8")
        (src / "sub").mkdir()
        (src / "sub" / "b.txt").write_text("b", encoding="utf-8")

        sess = _FakeSession()
        p._upload_local_dir(sess, str(src), "/dst/dir")

        mkdirs = [a[0] for n, a in sess.calls if n == "mkdir"]
        uploads = {os.path.basename(a[0]): a[1]
                   for n, a in sess.calls if n == "upload_with_progress"}
        self.assertIn("/dst/dir", mkdirs)          # 先建目标目录
        self.assertIn("/dst/dir/sub", mkdirs)      # 子目录用 posix 分隔符
        self.assertEqual(uploads["a.txt"], "/dst/dir/a.txt")
        self.assertEqual(uploads["b.txt"], "/dst/dir/sub/b.txt")

    def test_tar_fast_path_used_when_remote_has_tar(self):
        """远端有 tar → 整目录走 upload_dir_tar 单流上传，不逐文件 SFTP。"""
        p = self._panel()
        src = Path(tempfile.mkdtemp())
        (src / "a.txt").write_text("aa", encoding="utf-8")
        (src / "sub").mkdir()
        (src / "sub" / "b.txt").write_text("bbb", encoding="utf-8")

        sess = _FakeSession()

        def remote_has_tar():
            return True
        sess.remote_has_tar = remote_has_tar
        p._upload_local_dir(sess, str(src), "/dst/dir")

        names = [n for n, _ in sess.calls]
        self.assertIn("upload_dir_tar", names)
        self.assertNotIn("upload_with_progress", names)
        self.assertNotIn("mkdir", names)  # mkdir -p 由远端命令完成
        tar_call = next(a for n, a in sess.calls if n == "upload_dir_tar")
        self.assertEqual(tar_call[0], str(src))
        self.assertEqual(tar_call[1], "/dst/dir")
        self.assertEqual(tar_call[2], 2 + 3)  # total_bytes = 文件字节和


class TestUploadDirTarStream(_Base):
    """SSHSession.upload_dir_tar 的流正确性（假 channel，不碰网络）。"""

    def test_streams_valid_tar_and_quotes_command(self):
        import io
        import tarfile as tarfile_mod
        from ssh_session import SSHSession, HostConfig

        buf = bytearray()

        class _FakeChan:
            cmd = None
            write_closed = False

            def settimeout(self, t):
                pass

            def exec_command(self, cmd):
                self.cmd = cmd

            def sendall(self, data):
                buf.extend(data)

            def recv_stderr_ready(self):
                return False

            def recv_ready(self):
                return False

            def shutdown_write(self):
                self.write_closed = True

            def exit_status_ready(self):
                return True

            def recv_exit_status(self):
                return 0

            def close(self):
                pass

        chan = _FakeChan()

        class _FakeTransport:
            def open_session(self, timeout=None):
                return chan

            def is_active(self):
                return True

        class _FakeClient:
            def get_transport(self):
                return _FakeTransport()

        sess = SSHSession(HostConfig(alias="t", hostname="h"))
        try:
            sess._client = _FakeClient()

            src = Path(tempfile.mkdtemp())
            (src / "a.txt").write_text("hello", encoding="utf-8")
            (src / "sub").mkdir()
            (src / "sub" / "b.txt").write_text("world!", encoding="utf-8")

            progress = []
            sess.upload_dir_tar(str(src), "/dst/my dir", total_bytes=11,
                                progress_cb=lambda d, t: progress.append((d, t)))

            # 远端命令：mkdir -p + tar -x，目标路径经 shell 引号保护
            self.assertIn("mkdir -p '/dst/my dir'", chan.cmd)
            self.assertIn("tar -xpf - -C '/dst/my dir'", chan.cmd)
            self.assertTrue(chan.write_closed)

            # 灌出去的字节必须是合法 tar 且内容一致
            tf = tarfile_mod.open(fileobj=io.BytesIO(bytes(buf)), mode="r:")
            names = {m.name for m in tf.getmembers()}
            self.assertIn("./a.txt", names)
            self.assertIn("./sub/b.txt", names)
            self.assertEqual(tf.extractfile("./a.txt").read(), b"hello")
            self.assertEqual(tf.extractfile("./sub/b.txt").read(), b"world!")

            # 进度回调发生过，且 bytes_done 被 total 封顶
            self.assertTrue(progress)
            self.assertLessEqual(max(d for d, _ in progress), 11)
        finally:
            sess._executor.shutdown(wait=False)


class TestSortPersistence(_Base):
    def test_invalid_key_falls_back_to_name(self):
        p = self._panel()
        p.set_sort("bogus", True)
        self.assertEqual(p.get_sort(), ("name", True))

    def test_sort_persists_across_panels(self):
        p = self._panel()
        p.set_sort("size", True)
        p2 = self._panel()
        self.assertEqual(p2.get_sort(), ("size", True))


if __name__ == "__main__":
    unittest.main()
