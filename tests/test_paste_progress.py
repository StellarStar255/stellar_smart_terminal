"""一次粘贴一批文件时的统一进度窗口（本地 + 远程面板，不依赖真实 SSH）。

用户报告：远程一次粘贴几十个文件，进度框一个条目弹一次（远程→远程还
按 stat/download/upload 每个阶段各弹一次），窗口闪个不停，既看不出整体
进度，出错也说不清是哪一项。修复后一批粘贴只开一个列表窗口，每个条目
一行，逐行显示 等待 / 进行中 / 已完成 / 失败：原因。窗口能力收敛在
explorer_common.TransferJobHost，本地 explorer 与远程 explorer 共用。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_paste_progress.py
"""
import os
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeRemoteSession:
    """submit 立即完成的假会话；stat/listdir 返回可控结果，其余只记录。"""

    def __init__(self, alias="host", entries=(), stat_hook=None):
        self.alias = alias
        self.calls = []            # (fn_name, args)
        self._entries = list(entries)
        self._stat_hook = stat_hook
        self.aborted = 0

    # --- SSHSession 接口的最小子集 ---
    def submit(self, fn, *args):
        self.calls.append((fn.__name__, args))
        fut = Future()
        try:
            fut.set_result(fn(*args))
        except Exception as e:      # noqa: BLE001 — 照搬到 future 上
            fut.set_exception(e)
        return fut

    def is_connected(self):
        return True

    def listdir(self, path):
        return list(self._entries)

    def stat(self, path):
        if self._stat_hook is not None:
            self._stat_hook(path)
        from ssh_session import RemoteEntry
        return RemoteEntry(name=os.path.basename(path), path=path,
                           is_dir=False, size=4, mtime=0.0)

    def download(self, remote_path, local_path):
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(b"data")

    def upload_with_progress(self, local_path, remote_path, cb=None):
        return None

    def remote_has_tar(self):
        return False

    def upload_files_tar(self, items, remote_dir, total_bytes=0, cb=None,
                         should_stop=None):
        return []

    def abort(self):
        self.aborted += 1


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import app_config
        import explorer_widget as ew
        import remote_explorer_widget as rew
        from PyQt6.QtWidgets import QProgressDialog, QMessageBox
        from transfer_progress import TransferProgressDialog

        self._tmp_cfg = Path(tempfile.mkdtemp()) / "cfg.json"
        self._orig_cfg_path = app_config.get_config_path
        app_config.get_config_path = lambda: self._tmp_cfg

        self.rew = rew
        self.ew = ew
        # 逐条目弹框的计数器：真 QProgressDialog 的子类，sip 检查照常可用
        popups = []

        class _CountingProgressDialog(QProgressDialog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                popups.append(self)

        # 统一窗口：记录实例 + 收尾时快照每行状态（随后可能被 deleteLater）
        jobs = []

        class _RecordingJob(TransferProgressDialog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.final_status = None
                jobs.append(self)

            def finish_all(self):
                super().finish_all()
                self.final_status = list(self.row_states())

        self.popups = popups
        self.jobs = jobs
        # 统一窗口由 explorer_common 的 mixin 构造（本地/远程面板共用）
        self.explorer_common = rew.explorer_common
        self._orig_popup_cls = (rew.QProgressDialog, ew.QProgressDialog)
        self._orig_job_cls = self.explorer_common.TransferProgressDialog
        rew.QProgressDialog = _CountingProgressDialog
        ew.QProgressDialog = _CountingProgressDialog
        self.explorer_common.TransferProgressDialog = _RecordingJob

        self.warnings = []
        self._orig_warning = QMessageBox.warning
        QMessageBox.warning = lambda *a, **k: self.warnings.append(a[2:])
        self._qmessagebox = QMessageBox

    def tearDown(self):
        import app_config
        app_config.get_config_path = self._orig_cfg_path
        self.rew.QProgressDialog, self.ew.QProgressDialog = self._orig_popup_cls
        self.explorer_common.TransferProgressDialog = self._orig_job_cls
        self._qmessagebox.warning = self._orig_warning

    def _panel(self, sess):
        panel = self.rew.RemoteExplorerPanel(theme={})
        panel._session = sess
        panel._current_path = "/dst"
        panel._populate_tree_root = lambda: None
        panel._refresh_subtree_by_path = lambda path: None
        return panel

    def _paste(self, panel, items, target="/dst"):
        rew = self.rew
        orig = rew.explorer_clipboard.effective_items
        rew.explorer_clipboard.effective_items = lambda: items
        try:
            panel._clipboard_paste_into(target)
        finally:
            rew.explorer_clipboard.effective_items = orig

    def _local_files(self, n):
        d = Path(tempfile.mkdtemp())
        out = []
        for i in range(n):
            f = d / f"f{i}.bin"
            f.write_bytes(b"x" * (i + 1))
            out.append(("local", str(f)))
        return out


class TestUnifiedPasteWindow(_Base):
    def test_remote_paste_uses_one_window_instead_of_per_item_popups(self):
        """3 个远程条目 = 1 个列表窗口、0 个逐项进度框。

        修复前：每个条目的 stat / download / upload 各弹一个 QProgressDialog。
        """
        dst = _FakeRemoteSession(alias="dst")
        src = _FakeRemoteSession(alias="src")
        panel = self._panel(dst)
        items = [("remote", "src", f"/src/f{i}.txt", src) for i in range(3)]

        self._paste(panel, items)

        self.assertEqual(len(self.jobs), 1, "一批粘贴只该开一个统一窗口")
        self.assertEqual(self.popups, [],
                         "统一窗口在时不该再逐条目/逐阶段弹进度框")
        job = self.jobs[0]
        self.assertEqual(job.final_status, ["done"] * 3)

    def test_local_batch_paste_marks_every_row_done(self):
        dst = _FakeRemoteSession(alias="dst")
        panel = self._panel(dst)

        self._paste(panel, self._local_files(3))

        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.popups, [])
        self.assertEqual(self.jobs[0].final_status, ["done"] * 3)

    def test_failed_item_is_marked_on_its_own_row_and_batch_continues(self):
        """一项失败不该中断整批，也不该再叠一个错误弹窗——行上写清楚原因。"""
        def stat_hook(path):
            if path.endswith("f1.txt"):
                raise OSError("permission denied")

        dst = _FakeRemoteSession(alias="dst")
        src = _FakeRemoteSession(alias="src", stat_hook=stat_hook)
        panel = self._panel(dst)
        items = [("remote", "src", f"/src/f{i}.txt", src) for i in range(3)]

        self._paste(panel, items)

        job = self.jobs[0]
        self.assertEqual(job.final_status, ["done", "failed", "done"])
        self.assertIn("permission denied", job.row_status_text(1))
        self.assertEqual(self.warnings, [],
                         "失败已逐行显示，不该再弹一个汇总框")

    def test_cancel_skips_the_remaining_items(self):
        """点取消 → 余下条目标「已取消」，不继续偷偷传。

        注意是「优雅停」：不关 socket（那会让正在写的文件在远端留半截），
        只是不再开始新的条目。
        """
        state = {"panel": None}

        def stat_hook(path):
            if path.endswith("f0.txt"):
                # 模拟用户在第一项传输中点了「取消」
                state["panel"]._transfer_job._request_cancel()

        dst = _FakeRemoteSession(alias="dst")
        src = _FakeRemoteSession(alias="src", stat_hook=stat_hook)
        panel = self._panel(dst)
        state["panel"] = panel
        items = [("remote", "src", f"/src/f{i}.txt", src) for i in range(3)]

        self._paste(panel, items)

        job = self.jobs[0]
        self.assertEqual(job.final_status[1:], ["skipped", "skipped"])
        self.assertNotIn("/src/f2.txt", [a[0] for _n, a in src.calls if a],
                         "取消之后不该再碰后面的条目")
        self.assertEqual(src.aborted + dst.aborted, 0,
                         "优雅停不许关 socket——远端会留半截文件")


class TestOverwriteConflictStaysInTheWindow(_Base):
    """目标目录全是同名文件、用户选「覆盖并应用到剩余」时的删除阶段。

    这条最容易漏：每个文件覆盖前要 stat + remove 两次远端调用，标签是
    单个文件路径（用户截图里就是这种「Pasting into <某个文件>」），
    以前每一次都弹一个框。
    """

    def test_overwrite_deletes_do_not_pop_dialogs(self):
        class _Entry:
            def __init__(self, name):
                self.name = name

        src_dir = Path(tempfile.mkdtemp())
        paths = []
        for i in range(5):
            f = src_dir / f"f{i}.jsonl"
            f.write_bytes(b"x" * (i + 1))
            paths.append(str(f))

        dst = _FakeRemoteSession(
            alias="dst", entries=[_Entry(os.path.basename(p)) for p in paths])
        dst.remove = lambda path: None
        dst.remove_tree = lambda path: None
        panel = self._panel(dst)
        # 用户在第一次冲突框里选「覆盖」并勾了「应用到剩余」
        panel._resolve_paste_conflict = lambda name, sticky: ("overwrite", True)

        self._paste(panel, [("local", p) for p in paths])

        self.assertEqual(self.popups, [],
                         "覆盖前的 stat/remove 也该画进统一窗口，不许弹框")
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0].final_status, ["done"] * 5)


class TestOverwriteSkipsUselessDeletes(_Base):
    """覆盖普通文件不该先删：上传本身就是覆盖写。

    以前每个冲突文件都要 stat + remove 两趟 SSH 往返，粘 64 个就是 128 次
    往返全花在还没开始传数据的地方（用户看到一行行「检查中」慢慢爬）。
    """

    class _Ent:
        def __init__(self, name, is_dir=False, is_link=False):
            self.name = name
            self.is_dir = is_dir
            self.is_link = is_link
            self.path = "/dst/" + name

    def _paste_over(self, entries, n=3):
        src_dir = Path(tempfile.mkdtemp())
        items = []
        for i in range(n):
            f = src_dir / f"f{i}.jsonl"
            f.write_bytes(b"x" * (i + 1))
            items.append(("local", str(f)))
        dst = _FakeRemoteSession(alias="dst", entries=entries)
        dst.remove = lambda path: None
        dst.remove_tree = lambda path: None
        panel = self._panel(dst)
        panel._resolve_paste_conflict = lambda name, sticky: ("overwrite", True)
        self._paste(panel, items)
        return dst

    def test_plain_file_overwrite_does_no_stat_or_remove(self):
        entries = [self._Ent(f"f{i}.jsonl") for i in range(3)]
        dst = self._paste_over(entries)
        names = [n for n, _a in dst.calls]
        self.assertNotIn("stat", names, "普通文件覆盖不用先 stat")
        self.assertEqual([n for n in names if n.startswith("remove")], [],
                         "普通文件覆盖不用先删——上传就是覆盖写")
        self.assertTrue([n for n in names if n.startswith("upload")],
                        "该传的还是要传")

    def test_directory_in_the_way_is_still_removed(self):
        entries = [self._Ent("f0.jsonl", is_dir=True),
                   self._Ent("f1.jsonl"), self._Ent("f2.jsonl")]
        dst = self._paste_over(entries)
        names = [n for n, _a in dst.calls]
        self.assertIn("stat", names, "同名是目录 → 仍要走删除流程")

    def test_needs_delete_matrix(self):
        cls = self.rew.RemoteExplorerPanel
        self.assertFalse(cls._overwrite_needs_delete(self._Ent("a"), False))
        self.assertTrue(cls._overwrite_needs_delete(self._Ent("a", is_dir=True), False))
        self.assertTrue(cls._overwrite_needs_delete(self._Ent("a", is_link=True), False))
        self.assertTrue(cls._overwrite_needs_delete(self._Ent("a"), True))
        self.assertTrue(cls._overwrite_needs_delete(None, False),
                        "不知道对面是什么就保守地删")


class TestSingleItemKeepsSimpleDialog(_Base):
    def test_single_item_paste_still_uses_plain_progress_dialog(self):
        """只粘一个条目时列表窗反而啰嗦 —— 保持原来的单进度框。"""
        dst = _FakeRemoteSession(alias="dst")
        src = _FakeRemoteSession(alias="src")
        panel = self._panel(dst)

        self._paste(panel, [("remote", "src", "/src/only.txt", src)])

        self.assertEqual(self.jobs, [])
        self.assertGreater(len(self.popups), 0)


class TestLocalExplorerPasteWindow(_Base):
    """本地面板走同一套窗口：本地→本地复制、远端→本地下载都只开一个。"""

    def _local_panel(self):
        panel = self.ew.ExplorerPanel()
        panel.refresh = lambda: None
        return panel

    def _paste_local(self, panel, items, target):
        ew = self.ew
        orig = ew.explorer_clipboard.effective_items
        cut = ew.explorer_clipboard.is_cut
        ew.explorer_clipboard.effective_items = lambda: items
        ew.explorer_clipboard.is_cut = lambda: False
        try:
            panel._clipboard_paste_into(target)
        finally:
            ew.explorer_clipboard.effective_items = orig
            ew.explorer_clipboard.is_cut = cut

    def test_local_copy_batch_uses_one_window(self):
        panel = self._local_panel()
        target = tempfile.mkdtemp()
        items = self._local_files(3)

        self._paste_local(panel, items, target)

        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.jobs[0].final_status, ["done"] * 3)
        self.assertEqual(sorted(os.listdir(target)),
                         ["f0.bin", "f1.bin", "f2.bin"])

    def test_remote_to_local_paste_uses_one_window_not_per_file_popups(self):
        """修复前：每个远端条目的 stat / download 各弹一个进度框。"""
        src = _FakeRemoteSession(alias="src")
        panel = self._local_panel()
        target = tempfile.mkdtemp()
        items = [("remote", "src", f"/src/f{i}.txt", src) for i in range(3)]

        self._paste_local(panel, items, target)

        self.assertEqual(len(self.jobs), 1, "一批粘贴只该开一个统一窗口")
        self.assertEqual(self.popups, [],
                         "统一窗口在时不该再逐条目/逐阶段弹进度框")
        self.assertEqual(self.jobs[0].final_status, ["done"] * 3)

    def test_failed_download_lands_on_its_own_row(self):
        def stat_hook(path):
            if path.endswith("f1.txt"):
                raise OSError("no such file")

        src = _FakeRemoteSession(alias="src", stat_hook=stat_hook)
        panel = self._local_panel()
        target = tempfile.mkdtemp()
        items = [("remote", "src", f"/src/f{i}.txt", src) for i in range(3)]

        self._paste_local(panel, items, target)

        job = self.jobs[0]
        self.assertEqual(job.final_status, ["done", "failed", "done"])
        self.assertIn("no such file", job.row_status_text(1))
        self.assertEqual(self.warnings, [])


class TestPerRowBytes(_Base):
    """整批的字节数不能写进每一行 —— 用户看到 64 行一模一样的
    "129.0 MB / 162.8 MB · 3.1 MB/s"。行上要么是它自己的进度，要么是
    「进行中…」，整批统计归到阶段行。"""

    def _dialog(self, rows=("a", "b", "c")):
        from transfer_progress import TransferProgressDialog
        return TransferProgressDialog(list(rows), delay_ms=0,
                                      header="正在粘贴到 /dst…")

    def test_batch_detail_never_lands_on_every_row(self):
        from i18n import t
        dlg = self._dialog()
        dlg.set_active_rows([0, 1, 2])
        dlg.set_stage_progress(0.5, "129.0 MB / 162.8 MB · 3.1 MB/s")
        rows = [dlg.row_status_text(i) for i in range(3)]
        self.assertEqual(rows, [t("transfer.state_running")] * 3)
        self.assertIn("129.0 MB", dlg.stage_text())

    def test_single_active_row_keeps_its_own_detail(self):
        dlg = self._dialog()
        dlg.set_active_rows([1])
        dlg.set_stage_progress(0.5, "2.0 MB / 4.0 MB")
        self.assertEqual(dlg.row_status_text(1), "2.0 MB / 4.0 MB")

    def test_stage_line_does_not_repeat_the_header(self):
        dlg = self._dialog()
        dlg.set_stage("正在粘贴到 /dst…")          # 与 header 一字不差
        self.assertEqual(dlg.stage_text(), "")
        dlg.set_stage("正在下载 /src/a.jsonl…")     # 不同的阶段照常显示
        self.assertIn("a.jsonl", dlg.stage_text())


class TestStreamPosition(_Base):
    """按累计字节反推「正在传第几个文件」。"""

    def _pos(self, sizes, cur):
        from remote_explorer_widget import _stream_position
        prefix, acc = [], 0
        for s in sizes:
            prefix.append(acc)
            acc += s
        return _stream_position(prefix, sizes, cur)

    def test_maps_bytes_to_the_file_being_written(self):
        sizes = [100, 200, 50]
        self.assertEqual(self._pos(sizes, 0), (0, 0))
        self.assertEqual(self._pos(sizes, 60), (0, 60))
        self.assertEqual(self._pos(sizes, 100), (1, 0))
        self.assertEqual(self._pos(sizes, 250), (1, 150))
        self.assertEqual(self._pos(sizes, 300), (2, 0))

    def test_overshoot_and_empty_files_stay_in_range(self):
        # tar 头部/补齐会让累计字节略超总量；空文件不能把游标卡住
        self.assertEqual(self._pos([100, 200, 50], 10_000), (2, 50))
        # 空文件不占字节：走到 100 时它已经过去了，游标停在下一个
        self.assertEqual(self._pos([100, 0, 50], 100), (2, 0))
        self.assertEqual(self._pos([], 0), (0, 0))


class TestBatchRowsAdvance(_Base):
    """整批上传时行状态要跟着字节往前走：传过去的标完成、当前那个显示
    自己的字节、后面的还是等待中。"""

    def test_rows_advance_with_the_stream(self):
        from i18n import t
        src_dir = Path(tempfile.mkdtemp())
        pairs = []
        for i, n in enumerate((100, 200, 50)):
            f = src_dir / f"f{i}.bin"
            f.write_bytes(b"x" * n)
            pairs.append((str(f), f"/dst/f{i}.bin"))

        dst = _FakeRemoteSession(alias="dst")
        panel = self._panel(dst)
        job = self.explorer_common.TransferProgressDialog(
            ["f0.bin", "f1.bin", "f2.bin"], parent=panel, delay_ms=0,
            header="正在粘贴到 /dst…")
        panel._transfer_job = job

        # 用假的等待把 on_bytes 推到「第二个文件传了一半」，并在那一刻取快照
        # （真正传完后所有行都会落成「已完成」，看不到中途状态）
        snap = {}

        def fake_wait(futures, label, on_bytes=None, **kw):
            if on_bytes is not None:
                on_bytes(0)
                on_bytes(180)      # 100 + 80 → 第 1 个完成，第 2 个 80/200
                snap["states"] = job.row_states()
                snap["rows"] = [job.row_status_text(i) for i in range(3)]
        panel._wait_future_with_progress = fake_wait

        panel._upload_pairs(dst, pairs, "/dst", rows=[0, 1, 2])

        self.assertEqual(snap["states"][0], "done")
        self.assertIn("80 B / 200 B", snap["rows"][1])
        self.assertEqual(snap["rows"][2], t("transfer.state_pending"))
        # 三行的状态互不相同 —— 不是整批的同一个数字广播出去的
        self.assertEqual(len(set(snap["rows"])), 3)
        self.assertEqual(job.row_states(), ["done"] * 3)   # 传完全部落地


class TestHideAndReopen(_Base):
    """窗口要一直看得见（置顶），嫌挡事能收起，收起后能叫回来。"""

    def _dialog(self, rows=("a", "b")):
        from transfer_progress import TransferProgressDialog
        return TransferProgressDialog(list(rows), delay_ms=0)

    def test_window_stays_on_top(self):
        from PyQt6.QtCore import Qt
        dlg = self._dialog()
        self.assertTrue(bool(dlg.windowFlags()
                             & Qt.WindowType.WindowStaysOnTopHint))

    def test_hide_keeps_the_transfer_running(self):
        dlg = self._dialog()
        seen = []
        dlg.visibility_changed.connect(seen.append)
        dlg.canceled.connect(lambda: seen.append("canceled"))
        dlg.hide_for_now()
        self.assertFalse(dlg.isVisible())
        self.assertFalse(dlg.was_canceled(), "收起窗口不该把传输掐了")
        self.assertEqual(seen, [False])
        dlg.reopen()
        self.assertTrue(dlg.isVisible())
        self.assertEqual(seen, [False, True])

    def test_closing_hides_instead_of_canceling(self):
        dlg = self._dialog()
        dlg.close()
        self.assertFalse(dlg.isVisible())
        self.assertFalse(dlg.was_canceled())

    def test_delayed_show_respects_a_manual_hide(self):
        from transfer_progress import TransferProgressDialog
        dlg = TransferProgressDialog(["a", "b"], delay_ms=5000)
        dlg.hide_for_now()
        dlg._show_if_running()          # 延时显示的定时器到点了
        self.assertFalse(dlg.isVisible(), "收起过就别再自己弹出来")

    def test_failures_bring_a_hidden_window_back(self):
        dlg = self._dialog()
        dlg.hide_for_now()
        dlg.finish_row(0)
        dlg.finish_row(1, error="boom")
        dlg.finish_all()
        self.assertTrue(dlg.isVisible(), "有失败必须让人看见")

    def test_panel_button_appears_only_while_hidden(self):
        dst = _FakeRemoteSession(alias="dst")
        panel = self._panel(dst)
        job = panel._begin_transfer_job(["a", "b"], header="h")
        self.assertTrue(panel._transfer_chip.isHidden())
        job.hide_for_now()
        self.assertFalse(panel._transfer_chip.isHidden())
        panel._reopen_transfer_job()
        self.assertTrue(panel._transfer_chip.isHidden())
        job.hide_for_now()
        panel._end_transfer_job(job)     # 整批收尾 → 按钮灭掉
        self.assertTrue(panel._transfer_chip.isHidden())

    def test_local_panel_has_the_same_button(self):
        panel = self.ew.ExplorerPanel()
        panel.refresh = lambda: None
        job = panel._begin_transfer_job(["a", "b"], header="h")
        job.hide_for_now()
        self.assertFalse(panel._transfer_chip.isHidden())
        panel._end_transfer_job(job)
        self.assertTrue(panel._transfer_chip.isHidden())


class TestTopmostYieldsToModals(_Base):
    """置顶窗口不能盖住应用自己的模态框（冲突框/密码框），
    否则按钮点不到、整个粘贴卡死。"""

    def _dialog(self, rows=("a", "b")):
        from transfer_progress import TransferProgressDialog
        return TransferProgressDialog(list(rows), delay_ms=0)

    def _is_topmost(self, dlg):
        from PyQt6.QtCore import Qt
        return bool(dlg.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    def test_yields_by_hiding_not_by_touching_window_flags(self):
        """让位靠 hide/show，绝不改 window flag。

        改 flag 会销毁重建原生窗口，在模态状态下做这件事直接段错误
        （CI 上 macOS 与 Linux 各崩过一次）。
        """
        dlg = self._dialog()
        dlg.yield_for_modal()
        self.assertFalse(dlg.isVisible())
        self.assertTrue(self._is_topmost(dlg), "置顶标志自始至终不该动")
        dlg.restore_after_modal()
        self.assertTrue(dlg.isVisible())
        self.assertTrue(self._is_topmost(dlg))

    def test_user_hidden_window_is_left_alone(self):
        dlg = self._dialog()
        dlg.hide_for_now()                 # 用户主动收起
        dlg.yield_for_modal()
        dlg.restore_after_modal()
        self.assertFalse(dlg.isVisible(), "用户收起的窗口不该被模态框流程弹回来")

    def test_modal_state_drives_it(self):
        from PyQt6.QtWidgets import QApplication, QDialog
        dlg = self._dialog()
        self.assertTrue(dlg._modal_watch.isActive(),
                        "得有个定时器盯着模态状态，不然没人去让位")
        modal = QDialog()
        modal.setModal(True)
        modal.open()                       # 非阻塞地进入模态
        QApplication.processEvents()
        dlg._sync_with_modals()            # 定时器到点时做的事
        self.assertFalse(dlg.isVisible(), "模态框弹着的时候不能压在它上面")
        modal.close()
        QApplication.processEvents()
        dlg._sync_with_modals()
        self.assertTrue(dlg.isVisible(), "模态框关掉要回来")

    def test_conflict_dialog_suspends_the_transfer_window(self):
        """弹冲突框期间传输窗口必须让出置顶，关掉再还回去。"""
        import explorer_common
        dst = _FakeRemoteSession(alias="dst")
        panel = self._panel(dst)
        job = panel._begin_transfer_job(["a", "b"], header="h")
        job.show()          # 正常情况下是 300ms 延时显示的，这里直接显示

        seen = {}
        orig_box = explorer_common.QMessageBox

        outer = self

        class _Box(orig_box):
            def exec(self):               # noqa: A003 — 覆盖 Qt 接口
                seen["visible_during"] = job.isVisible()
                return 0

        explorer_common.QMessageBox = _Box
        try:
            explorer_common.resolve_paste_conflict(panel, "a.txt", None)
        finally:
            explorer_common.QMessageBox = orig_box

        self.assertFalse(seen["visible_during"],
                         "模态框弹着的时候传输窗口不能还压在上面")
        self.assertTrue(job.isVisible(), "关掉之后窗口要回来")


class TestQueuedRowsSayWaiting(_Base):
    """排队等着上传的行不能显示「进行中」——它压根还没开始传。

    用户报告：粘 64 个文件，扫过的十几行全是 "In progress..."，标题却是
    0/64 done。那些行只是解完冲突在排队。
    """

    def test_queued_local_files_go_back_to_waiting(self):
        from i18n import t
        dst = _FakeRemoteSession(alias="dst")
        panel = self._panel(dst)
        seen = {}

        # 在最后的批量上传之前抓一张快照：此刻所有行都只是排过队
        def fake_upload_pairs(sess, pairs, target_dir, rows=None):
            job = panel._transfer_job
            seen["rows"] = [job.row_status_text(i) for i in range(len(pairs))]
            return []
        panel._upload_pairs = fake_upload_pairs

        self._paste(panel, self._local_files(4))

        self.assertEqual(seen["rows"], [t("transfer.state_pending")] * 4)

    def test_requeue_only_touches_running_rows(self):
        from i18n import t
        from transfer_progress import TransferProgressDialog
        dlg = TransferProgressDialog(["a", "b"], delay_ms=0)
        dlg.set_active_rows([0])
        dlg.requeue_row(0)
        self.assertEqual(dlg.row_states()[0], "pending")
        self.assertEqual(dlg.row_status_text(0), t("transfer.state_pending"))
        dlg.finish_row(1)
        dlg.requeue_row(1)                      # 已完成的行不许被退回
        self.assertEqual(dlg.row_states()[1], "done")


class TestGracefulCancel(_Base):
    """点取消要先把正在写的那个文件传完，别在远端留半截。

    用户要求：「请等在正在上传的那个传完再停止（否则文件可能损坏），
    除非我坚持退出」——所以第一下是优雅停，再按一次才强制断。
    """

    def _dialog(self, rows=("a", "b")):
        from transfer_progress import TransferProgressDialog
        return TransferProgressDialog(list(rows), delay_ms=0)

    def test_first_press_is_graceful_second_is_force(self):
        from i18n import t
        dlg = self._dialog()
        soft, hard = [], []
        dlg.canceled.connect(lambda: soft.append(1))
        dlg.force_canceled.connect(lambda: hard.append(1))

        dlg._on_button()
        self.assertEqual((len(soft), len(hard)), (1, 0), "第一下只请求优雅停")
        self.assertTrue(dlg.was_canceled())
        self.assertFalse(dlg.was_force_canceled())
        self.assertEqual(dlg._button.text(), t("transfer.force_stop"))
        self.assertIn("停止", dlg.stage_text())

        dlg._on_button()
        self.assertEqual((len(soft), len(hard)), (1, 1), "再按一次才强制断")
        self.assertTrue(dlg.was_force_canceled())

    def test_panel_aborts_sessions_only_on_force(self):
        dst = _FakeRemoteSession(alias="dst")
        panel = self._panel(dst)
        job = panel._begin_transfer_job(["a", "b"], header="h")
        panel._register_job_abort(job, [dst])

        job._on_button()                     # 优雅停
        self.assertEqual(dst.aborted, 0,
                         "优雅停绝不能关 socket——远端会留半截文件")
        job._on_button()                     # 强制停
        self.assertGreater(dst.aborted, 0, "坚持退出就该真断")

    def test_tar_batch_gets_a_stop_callback(self):
        """tar 流靠 should_stop 在两个文件之间收尾，而不是被掐断。"""
        src_dir = Path(tempfile.mkdtemp())
        pairs = []
        for i in range(3):
            f = src_dir / f"f{i}.bin"
            f.write_bytes(b"x" * (i + 1))
            pairs.append((str(f), f"/dst/f{i}.bin"))

        seen = {}

        class _TarSession(_FakeRemoteSession):
            def remote_has_tar(self):
                return True

            def upload_files_tar(self, items, remote_dir, total_bytes=0,
                                 cb=None, should_stop=None):
                seen["should_stop"] = should_stop
                return []

        dst = _TarSession(alias="dst")
        panel = self._panel(dst)
        job = panel._begin_transfer_job(["f0", "f1", "f2"], header="h")
        panel._wait_future_with_progress = lambda *a, **k: None

        panel._upload_pairs(dst, pairs, "/dst", rows=[0, 1, 2])

        self.assertIsNotNone(seen.get("should_stop"), "得把停止回调传下去")
        self.assertFalse(seen["should_stop"](), "没点取消时不该停")
        job._on_button()
        self.assertTrue(seen["should_stop"](), "点了取消就在下个文件前停下")


class TestDialogLifecycle(_Base):
    """窗口自身的收尾规矩：全绿自动关，有失败留窗，且永远关得掉。"""

    def _dialog(self, rows=("a", "b")):
        from transfer_progress import TransferProgressDialog
        return TransferProgressDialog(list(rows), delay_ms=0)

    def test_all_done_closes_itself(self):
        dlg = self._dialog()
        dlg.finish_row(0)
        dlg.finish_row(1)
        dlg.finish_all()
        self.assertFalse(dlg.isVisible())

    def test_failures_keep_the_window_open(self):
        from i18n import t
        dlg = self._dialog()
        dlg.finish_row(0)
        dlg.finish_row(1, error="boom")
        dlg.finish_all()
        self.assertTrue(dlg.isVisible())
        self.assertEqual(dlg.failures(), {1: "boom"})
        self.assertEqual(dlg._button.text(), t("transfer.close"))
        dlg.close()                      # 收尾后关得掉
        self.assertFalse(dlg.isVisible())

    def test_cancel_button_aborts_without_closing_the_window(self):
        """取消是「取消」按钮的事：置位 + 发信号，窗口留着看收尾状态。"""
        dlg = self._dialog()
        seen = []
        dlg.canceled.connect(lambda: seen.append(1))
        dlg._on_button()
        self.assertTrue(dlg.was_canceled())
        self.assertEqual(seen, [1])
        self.assertTrue(dlg.isVisible())

    def test_overall_bar_counts_settled_rows_plus_current_fraction(self):
        from transfer_progress import BAR_SCALE
        dlg = self._dialog(("a", "b", "c", "d"))
        dlg.finish_row(0)
        dlg.set_active_rows([1])
        dlg.set_stage_progress(0.5)
        # 1 项完成 + 当前项一半 = 1.5/4
        self.assertEqual(dlg.overall_value(), int(1.5 / 4 * BAR_SCALE))


class TestClipboardItemNames(_Base):
    def test_names_shown_in_the_list(self):
        panel = self._panel(_FakeRemoteSession())
        self.assertEqual(panel._clipboard_item_name(("local", "/a/b/c.txt")),
                         "c.txt")
        self.assertEqual(
            panel._clipboard_item_name(("remote", "srv", "/x/y/z.log", None)),
            "srv:z.log")


if __name__ == "__main__":
    unittest.main()


class TestBothBackendsShareTheSameContract(_Base):
    """两个后端（paramiko / ControlMaster）的传输接口签名必须一模一样。

    实战事故：给 ssh_session 的 upload_files_tar 加了 should_stop，忘了
    ControlMaster 那个同名方法 —— 用户那台主机正好走 ControlMaster，整批
    粘贴当场 TypeError 全挂。面板是按同一套接口调两个后端的，签名分叉
    编译期发现不了，只能靠这个测试盯着。
    """

    def test_transfer_methods_have_identical_signatures(self):
        import inspect
        from ssh_session import SSHSession
        from ssh_control import ControlMasterSession
        for name in ("upload_files_tar", "upload_dir_tar",
                     "upload_with_progress", "download", "remote_has_tar",
                     "remote_has_gzip"):
            with self.subTest(method=name):
                a = inspect.signature(getattr(SSHSession, name))
                b = inspect.signature(getattr(ControlMasterSession, name))
                self.assertEqual(str(a), str(b), f"{name} 两个后端签名分叉了")


class TestFailedBatchDoesNotLookDone(_Base):
    """整批失败时不能留下一屏「已完成」——用户看到 63 个 Done 其实一个没传。"""

    def test_batch_error_marks_every_row_failed(self):
        src_dir = Path(tempfile.mkdtemp())
        pairs = []
        for i in range(4):
            f = src_dir / f"f{i}.jsonl"
            f.write_bytes(b"x" * 100)
            pairs.append((str(f), f"/dst/f{i}.jsonl"))

        class _BoomSession(_FakeRemoteSession):
            def remote_has_tar(self):
                return True

            def upload_files_tar(self, items, remote_dir, total_bytes=0,
                                 cb=None, should_stop=None):
                raise TypeError("upload_files_tar() takes 5 but 6 were given")

        dst = _BoomSession(alias="dst")
        panel = self._panel(dst)
        job = panel._begin_transfer_job([f"f{i}" for i in range(4)], header="h")

        # 模拟等待期间「乐观」按字节标完成（真实场景里就是这么标出来的）
        def fake_wait(futures, label, on_bytes=None, **kw):
            if on_bytes is not None:
                on_bytes(400)          # 全部字节到位 → 前几行会被标 Done
        panel._wait_future_with_progress = fake_wait

        errors = panel._upload_pairs(dst, pairs, "/dst", rows=[0, 1, 2, 3])

        self.assertTrue(errors, "整批失败要有错误")
        self.assertEqual(job.row_states(), ["failed"] * 4,
                         "整批失败时每一行都得是失败，不能留着乐观的已完成")
