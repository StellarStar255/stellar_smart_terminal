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

    has_tar 默认 False → 走逐文件兜底路径；置 True 可验证 tar 批量快路径。
    """

    def __init__(self, has_tar=False):
        self.calls = []          # (fn_name, args)
        self.has_tar = has_tar

    def submit(self, fn, *args):
        self.calls.append((fn.__name__, args))
        fut = Future()
        # 探测类调用要真实返回结果，其余只记录不执行
        if fn.__name__ in ("remote_has_tar", "upload_files_tar"):
            fut.set_result(fn(*args))
        else:
            fut.set_result(None)
        return fut

    def remote_has_tar(self):
        return self.has_tar

    def upload_files_tar(self, items, remote_dir, total_bytes=0,
                         progress_cb=None, should_stop=None):
        return []

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


class TestProgressBarBytes(_Base):
    """进度条按字节推进的回归测试。

    历史 bug：进度条刻度是「已完成的 future 数」，单任务传输（整目录 tar
    流、单个大文件）永远是 0/1 —— 文字里字节数在涨，条子却全程空着，
    传完瞬间才跳满。
    """

    def _run_with_live_bytes(self, panel, sizes):
        """跑一次 _wait_future_with_progress，中途采样进度条的值。"""
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QProgressDialog

        fut = Future()
        live = {"bytes": 0}
        sampled = {}

        def feed():
            live["bytes"] = 50           # 100 字节总量的一半

        def sample_and_finish():
            dlgs = panel.findChildren(QProgressDialog)
            if dlgs:
                sampled["value"] = dlgs[0].value()
                sampled["max"] = dlgs[0].maximum()
            fut.set_result(None)

        QTimer.singleShot(60, feed)
        QTimer.singleShot(260, sample_and_finish)   # 中间至少跑过一次 tick
        panel._wait_future_with_progress([fut], "x", sizes=sizes, live=live)
        return sampled

    def test_bar_advances_with_bytes_on_single_transfer(self):
        p = self._panel()
        sampled = self._run_with_live_bytes(p, sizes=[100])
        self.assertTrue(sampled, "未采样到进度对话框")
        # 传了一半 → 进度条应在中点附近，而不是停在 0
        self.assertGreater(sampled["max"], 1,
                           "总字节已知时刻度不应是「任务数」")
        ratio = sampled["value"] / sampled["max"]
        self.assertAlmostEqual(ratio, 0.5, delta=0.05,
                               msg=f"进度条未随字节推进: {sampled}")

    def test_bar_falls_back_to_task_count_without_sizes(self):
        """总字节未知（无 sizes）时退化为按任务数计数，不应崩。"""
        p = self._panel()
        sampled = self._run_with_live_bytes(p, sizes=None)
        self.assertEqual(sampled.get("max"), 1)
        self.assertEqual(sampled.get("value"), 0)   # 唯一任务尚未完成


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
            self.assertRegex(chan.cmd, r"tar -xz?pf - -C '/dst/my dir'")
            self.assertTrue(chan.write_closed)

            # 灌出去的字节必须是合法 tar 且内容一致（流可能是压缩的）
            gz = bytes(buf[:2]) == b"\x1f\x8b"
            tf = tarfile_mod.open(fileobj=io.BytesIO(bytes(buf)),
                                  mode="r:gz" if gz else "r:")
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


class _TarStreamHarness:
    """收集 SSHSession 往通道里灌的 tar 字节（假 channel，不碰网络）。"""

    def __init__(self):
        self.buf = bytearray()
        harness = self

        class _FakeChan:
            cmd = None
            write_closed = False

            def settimeout(self, t):
                pass

            def exec_command(self, cmd):
                self.cmd = cmd

            def sendall(self, data):
                harness.buf.extend(data)

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

        self.chan = _FakeChan()

        class _FakeTransport:
            def open_session(self_inner, timeout=None):
                return harness.chan

            def is_active(self_inner):
                return True

        class _FakeClient:
            def get_transport(self_inner):
                return _FakeTransport()

        self.client = _FakeClient()

    def is_gzip(self):
        return bytes(self.buf[:2]) == b"\x1f\x8b"

    def members(self):
        import io
        import tarfile as tarfile_mod
        # 流可能是压缩的（文本批次默认压缩上传），按 magic 自动判断
        mode = "r:gz" if self.is_gzip() else "r:"
        tf = tarfile_mod.open(fileobj=io.BytesIO(bytes(self.buf)), mode=mode)
        return {m.name: tf for m in tf.getmembers()}, tf


class TestUploadFilesTar(_Base):
    """多文件批量上传：一条 tar 流，而不是 N 次逐文件往返。"""

    def _session(self, harness):
        from ssh_session import SSHSession, HostConfig
        sess = SSHSession(HostConfig(alias="t", hostname="h"))
        sess._client = harness.client
        return sess

    def test_batch_lands_as_one_tar_with_basenames(self):
        harness = _TarStreamHarness()
        sess = self._session(harness)
        try:
            src = Path(tempfile.mkdtemp())
            (src / "a.txt").write_text("hello", encoding="utf-8")
            (src / "b b.txt").write_text("spaces", encoding="utf-8")
            (src / "dir").mkdir()
            (src / "dir" / "c.txt").write_text("nested", encoding="utf-8")

            progress = []
            skipped = sess.upload_files_tar(
                [str(src / "a.txt"), str(src / "b b.txt"), str(src / "dir")],
                "/dst/my target", total_bytes=17,
                progress_cb=lambda d, t: progress.append((d, t)))

            self.assertEqual(skipped, [])
            # 目标路径经 shell 引号保护（带空格也不会被拆开）
            self.assertRegex(harness.chan.cmd,
                             r"tar -xz?pf - -C '/dst/my target'")
            names, tf = harness.members()
            # 归档里用 basename：拖进来的东西直接落在目标目录下
            self.assertIn("a.txt", names)
            self.assertIn("b b.txt", names)
            self.assertIn("dir/c.txt", names)
            self.assertEqual(tf.extractfile("a.txt").read(), b"hello")
            self.assertEqual(tf.extractfile("dir/c.txt").read(), b"nested")
            self.assertTrue(progress)
            self.assertLessEqual(max(d for d, _ in progress), 17)
        finally:
            sess._executor.shutdown(wait=False)

    def test_unreadable_entry_is_skipped_not_fatal(self):
        """一个读不了的文件不该让另外 299 个也传不上去。"""
        harness = _TarStreamHarness()
        sess = self._session(harness)
        try:
            src = Path(tempfile.mkdtemp())
            (src / "ok.txt").write_text("fine", encoding="utf-8")
            missing = str(src / "gone.txt")

            skipped = sess.upload_files_tar([str(src / "ok.txt"), missing],
                                            "/dst", total_bytes=4)
            self.assertEqual([p for p, _why in skipped], [missing])
            names, tf = harness.members()
            self.assertIn("ok.txt", names)
        finally:
            sess._executor.shutdown(wait=False)


class TestTarCompression(_Base):
    """慢链路上最大的一块：文本批次压着传，字节数直接砍掉几倍。"""

    def _session(self, harness, has_gzip=True):
        from ssh_session import SSHSession, HostConfig
        sess = SSHSession(HostConfig(alias="t", hostname="h"))
        sess._client = harness.client
        sess._remote_has_gzip = has_gzip
        return sess

    def _batch(self, sess, harness, names, payload=b"x" * 4096):
        src = Path(tempfile.mkdtemp())
        paths = []
        for n in names:
            f = src / n
            f.write_bytes(payload)
            paths.append(str(f))
        sess.upload_files_tar(paths, "/dst", total_bytes=len(payload) * len(paths))
        return paths

    def test_text_batch_is_compressed(self):
        harness = _TarStreamHarness()
        sess = self._session(harness)
        try:
            self._batch(sess, harness, ["a.jsonl", "b.jsonl"])
            self.assertIn("tar -xzpf -", harness.chan.cmd)
            self.assertTrue(harness.is_gzip(), "文本批次该走压缩流")
            names, tf = harness.members()
            self.assertEqual(sorted(names), ["a.jsonl", "b.jsonl"])
            self.assertEqual(len(tf.extractfile("a.jsonl").read()), 4096)
            # 可压内容：上线字节远小于原始字节
            self.assertLess(len(harness.buf), 4096 * 2)
        finally:
            sess._executor.shutdown(wait=False)

    def test_already_compressed_batch_stays_plain(self):
        """一堆 mp4 再 gzip 一遍纯属白烧 CPU。"""
        harness = _TarStreamHarness()
        sess = self._session(harness)
        try:
            self._batch(sess, harness, ["a.mp4", "b.mp4"])
            self.assertIn("tar -xpf -", harness.chan.cmd)
            self.assertFalse(harness.is_gzip())
        finally:
            sess._executor.shutdown(wait=False)

    def test_no_remote_gzip_falls_back_to_plain(self):
        harness = _TarStreamHarness()
        sess = self._session(harness, has_gzip=False)
        try:
            self._batch(sess, harness, ["a.jsonl"])
            self.assertIn("tar -xpf -", harness.chan.cmd)
            self.assertFalse(harness.is_gzip())
        finally:
            sess._executor.shutdown(wait=False)

    def test_progress_counts_original_bytes_not_wire_bytes(self):
        """压缩后通道字节远小于文件字节；进度必须按原始字节报，
        否则条子提前跑满、逐文件定位也全错。"""
        harness = _TarStreamHarness()
        sess = self._session(harness)
        try:
            src = Path(tempfile.mkdtemp())
            (src / "a.jsonl").write_bytes(b"x" * 8192)
            seen = []
            sess.upload_files_tar([str(src / "a.jsonl")], "/dst",
                                  total_bytes=8192,
                                  progress_cb=lambda d, t: seen.append(d))
            self.assertTrue(seen)
            self.assertEqual(max(seen), 8192)
            self.assertLess(len(harness.buf), 8192, "压缩流本身应该小得多")
        finally:
            sess._executor.shutdown(wait=False)


class TestUploadBatching(_Base):
    """面板决定"走 tar 单流还是逐文件"的分流逻辑。"""

    def _panel_with_session(self, has_tar=True):
        from concurrent.futures import Future
        panel = self._panel()

        class _Sess:
            host_config = None

            def __init__(self):
                self.tar_calls = []
                self.file_calls = []

            def submit(self, fn, *a, **k):
                fut = Future()
                try:
                    fut.set_result(fn(*a, **k))
                except Exception as e:      # pragma: no cover
                    fut.set_exception(e)
                return fut

            def remote_has_tar(self):
                return has_tar

            def upload_files_tar(self, paths, dst, total=0, cb=None):
                self.tar_calls.append((list(paths), dst))
                return []

            def upload_with_progress(self, local, remote, cb=None):
                self.file_calls.append((local, remote))

            def abort(self):
                pass

        sess = _Sess()
        panel._session = sess
        # 进度框/刷新都不参与本用例的判定
        panel._wait_future_with_progress = lambda *a, **k: None
        panel._refresh_upload_target = lambda *a, **k: None
        return panel, sess

    def _files(self, n):
        d = Path(tempfile.mkdtemp())
        out = []
        for i in range(n):
            p = d / f"f{i}.bin"
            p.write_bytes(b"x" * 16)
            out.append(str(p))
        return out

    def test_many_files_go_through_a_single_tar_stream(self):
        panel, sess = self._panel_with_session(has_tar=True)
        paths = self._files(5)
        panel._upload_paths(paths, "/dst", None)
        self.assertEqual(len(sess.tar_calls), 1, "5 个文件应该只有一条 tar 流")
        self.assertEqual(sess.tar_calls[0][0], paths)
        self.assertEqual(sess.file_calls, [], "不该再逐文件上传")

    def test_single_file_keeps_the_direct_path(self):
        """单文件走老路：进度和错误信息更直接，也省掉打包开销。"""
        panel, sess = self._panel_with_session(has_tar=True)
        panel._upload_paths(self._files(1), "/dst", None)
        self.assertEqual(sess.tar_calls, [])
        self.assertEqual(len(sess.file_calls), 1)

    def test_folder_drop_uses_tar_even_alone(self):
        """以前拖文件夹会直接报错（SFTP put 一个目录）；现在走 tar。"""
        panel, sess = self._panel_with_session(has_tar=True)
        d = Path(tempfile.mkdtemp())
        (d / "inner").mkdir()
        (d / "inner" / "x.txt").write_text("hi", encoding="utf-8")
        panel._upload_paths([str(d / "inner")], "/dst", None)
        self.assertEqual(len(sess.tar_calls), 1)
        self.assertEqual(sess.file_calls, [])

    def test_falls_back_per_file_when_remote_has_no_tar(self):
        panel, sess = self._panel_with_session(has_tar=False)
        paths = self._files(3)
        panel._upload_paths(paths, "/dst", None)
        self.assertEqual(sess.tar_calls, [])
        self.assertEqual(len(sess.file_calls), 3)


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


class TestClipboardPasteBatch(_Base):
    """多文件粘贴上传必须合并为一个整体进度，而不是一个文件弹一个进度框。

    用户报告：远程一次粘贴多个文件时逐个显示进度。修复后普通文件在冲突
    解析完成后攒成一批、单次 _wait_future_with_progress（带 sizes 总量）。
    """

    def _paste_files(self, n_files, has_tar=False):
        """粘贴 n 个本地文件到假会话，返回 (带 sizes 的进度调用列表, 会话)。"""
        import remote_explorer_widget as rew
        p = self._panel()
        sess = _FakeSession(has_tar=has_tar)
        p._session = sess
        p._current_path = "/dst"

        src_dir = Path(tempfile.mkdtemp())
        paths = []
        for i in range(n_files):
            f = src_dir / f"f{i}.bin"
            f.write_bytes(b"x" * (i + 1))
            paths.append(("local", str(f)))

        progress_calls = []
        orig_wait = p._wait_future_with_progress

        def spy_wait(futures, label, tolerate_errors=False, sizes=None,
                     live=None, abort_sessions=None, on_bytes=None):
            if sizes is not None:
                progress_calls.append((list(futures), list(sizes)))
                return  # futures 已即刻完成，无需真跑事件循环
            return orig_wait(futures, label, tolerate_errors=tolerate_errors,
                             sizes=sizes, live=live,
                             abort_sessions=abort_sessions, on_bytes=on_bytes)

        p._wait_future_with_progress = spy_wait
        p._populate_tree_root = lambda: None
        p._refresh_subtree_by_path = lambda path: None

        orig_items = rew.explorer_clipboard.effective_items
        rew.explorer_clipboard.effective_items = lambda: paths
        try:
            p._clipboard_paste_into("/dst")
        finally:
            rew.explorer_clipboard.effective_items = orig_items
        return progress_calls, sess

    def test_multi_file_paste_uses_one_tar_stream(self):
        """粘贴一批文件和拖进来一批是同一件事——都该走 tar 单流。"""
        progress_calls, sess = self._paste_files(3, has_tar=True)
        tar_calls = [a for n, a in sess.calls if n == "upload_files_tar"]
        self.assertEqual(len(tar_calls), 1, "3 个文件应合并成一条 tar 流")
        items, remote_dir = tar_calls[0][0], tar_calls[0][1]
        self.assertEqual(remote_dir, "/dst")
        # 每项是 (本地路径, 归档名)，归档名取目标名（冲突改名后也对得上）
        self.assertEqual(sorted(arc for _src, arc in items),
                         ["f0.bin", "f1.bin", "f2.bin"])
        self.assertFalse([n for n, _a in sess.calls if n == "upload_with_progress"],
                         "走了 tar 就不该再逐文件上传")
        self.assertEqual(len(progress_calls), 1)

    def test_renamed_target_keeps_its_new_name_in_the_archive(self):
        """冲突改名成 "x (2).txt" 时，tar 里的归档名必须是改过的那个。"""
        p = self._panel()
        sess = _FakeSession(has_tar=True)
        p._wait_future_with_progress = lambda *a, **k: None
        src = Path(tempfile.mkdtemp()) / "x.txt"
        src.write_text("hi", encoding="utf-8")
        errors = p._upload_pairs(sess, [(str(src), "/dst/x (2).txt"),
                                        (str(src), "/dst/x (3).txt")], "/dst")
        self.assertEqual(errors, [])
        items = [a for n, a in sess.calls if n == "upload_files_tar"][0][0]
        self.assertEqual([arc for _s, arc in items], ["x (2).txt", "x (3).txt"])

    def test_multi_file_paste_falls_back_per_file_without_tar(self):
        progress_calls, sess = self._paste_files(3)

        uploads = [a for n, a in sess.calls if n == "upload_with_progress"]
        self.assertEqual(len(uploads), 3)
        self.assertEqual({a[1] for a in uploads},
                         {"/dst/f0.bin", "/dst/f1.bin", "/dst/f2.bin"})
        # 核心断言：只有一个带总量的进度框，覆盖全部 3 个文件的字节数
        self.assertEqual(len(progress_calls), 1,
                         "多文件粘贴应合并为一个整体进度，而不是逐文件弹框")
        futures, sizes = progress_calls[0]
        self.assertEqual(len(futures), 3)
        self.assertEqual(sorted(sizes), [1, 2, 3])

    def test_single_file_paste_still_shows_progress(self):
        progress_calls, sess = self._paste_files(1)
        self.assertEqual(len(progress_calls), 1)
        futures, sizes = progress_calls[0]
        self.assertEqual(len(futures), 1)
        self.assertEqual(sizes, [1])


if __name__ == "__main__":
    unittest.main()
