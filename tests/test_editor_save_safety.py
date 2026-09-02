"""编辑器保存安全性测试（修复 #1 退出保存、#2 原子写）。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_editor_save_safety -v
"""
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._areas = []

    def tearDown(self):
        # 每个 EditorArea 打开文件后装了 QFileSystemWatcher；os.utime 触发的
        # fileChanged 信号会异步排队。若不销毁，这些信号会残留到事件队列，
        # 在后续测试（如 git 面板）processEvents 时投递 → 在已改动的 editor
        # 上弹模态框 → 离屏环境 abort，污染整个 suite。
        # 关键：先把每个 pane 的 watcher 彻底断开（移除路径 + 断信号），
        # 否则光 deleteLater + processEvents 反而会在此处投递信号弹模态。
        for area in self._areas:
            for pane in list(getattr(area, '_panes', [])):
                fw = getattr(pane, '_file_watcher', None)
                if fw is not None:
                    try:
                        files = fw.files()
                        if files:
                            fw.removePaths(files)
                        fw.fileChanged.disconnect()
                    except (TypeError, RuntimeError):
                        pass
            try:
                area.deleteLater()
            except Exception:
                pass
        self._areas = []
        self.app.processEvents()

    def _area_with_file(self, content="original\n"):
        from file_editor import EditorArea
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.txt")
        with open(p, "w") as f:
            f.write(content)
        area = EditorArea(theme={})
        self.assertTrue(area.open_file_in_active(p))
        self._areas.append(area)
        return area, area.active_pane, p

    def _make_disk_newer(self, pane, p, content):
        """模拟「文件在磁盘上被别人改新了」：写入新内容并把 mtime 推到未来。"""
        with open(p, "w") as f:
            f.write(content)
        future = time.time() + 10
        os.utime(p, (future, future))


class TestAtomicSave(_Base):
    def test_save_writes_correct_content(self):
        area, pane, p = self._area_with_file()
        pane.editor.setPlainText("edited\n")
        self.assertTrue(pane.is_modified())
        self.assertTrue(pane.save_file())
        with open(p) as f:
            self.assertEqual(f.read(), "edited\n")
        self.assertFalse(pane.is_modified())

    def test_save_is_atomic_replace(self):
        area, pane, p = self._area_with_file()
        ino = os.stat(p).st_ino
        pane.editor.setPlainText("v2\n")
        self.assertTrue(pane.save_file())
        # os.replace 换 inode → 证明走的是临时文件+rename，而非原地 truncate
        self.assertNotEqual(os.stat(p).st_ino, ino)

    def test_failed_write_keeps_original_and_cleans_tmp(self):
        area, pane, p = self._area_with_file("keepme\n")
        orig_bytes = open(p, "rb").read()
        pane.editor.setPlainText("should not land\n")

        # 让 os.replace 抛错，模拟写入最后一步失败
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated replace failure")

        # 静默 save 失败弹窗
        from PyQt6.QtWidgets import QMessageBox
        orig_warn = QMessageBox.warning
        QMessageBox.warning = staticmethod(lambda *a, **k: None)
        os.replace = boom
        try:
            self.assertFalse(pane.save_file())  # 失败返回 False
        finally:
            os.replace = real_replace
            QMessageBox.warning = orig_warn

        # 原文件原封不动；目录里没有残留临时文件
        self.assertEqual(open(p, "rb").read(), orig_bytes)
        leftovers = [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        # _original_content 未被更新 → 仍是"已修改"，缓冲区数据没丢
        self.assertTrue(pane.is_modified())


class TestQuitSaveGuard(_Base):
    def test_no_changes_proceeds(self):
        area, pane, p = self._area_with_file()
        self.assertFalse(area.has_unsaved())
        self.assertTrue(area.prompt_save_all())  # 无改动 → 直接放行，不弹窗

    def test_cancel_aborts_close(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file()
        pane.editor.setPlainText("unsaved\n")
        self.assertTrue(area.has_unsaved())
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Cancel)
        try:
            self.assertFalse(area.prompt_save_all())  # 取消 → 中止关闭
        finally:
            QMessageBox.question = orig
        # 取消后内容仍在
        self.assertEqual(pane.editor.toPlainText(), "unsaved\n")

    def test_save_choice_persists(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file()
        pane.editor.setPlainText("saved on quit\n")
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Save)
        try:
            self.assertTrue(area.prompt_save_all())
        finally:
            QMessageBox.question = orig
        with open(p) as f:
            self.assertEqual(f.read(), "saved on quit\n")
        self.assertFalse(pane.is_modified())

    def test_discard_proceeds_without_writing(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("disk\n")
        pane.editor.setPlainText("discarded\n")
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Discard)
        try:
            self.assertTrue(area.prompt_save_all())  # 丢弃 → 放行
        finally:
            QMessageBox.question = orig
        with open(p) as f:
            self.assertEqual(f.read(), "disk\n")  # 磁盘未被写

    def test_flush_autosave_writes_backup(self):
        area, pane, p = self._area_with_file()
        pane.editor.setPlainText("crash recovery\n")
        # 强制路径：不弹窗刷备份
        area.flush_autosave_all()
        paths = pane._backup_paths()
        self.assertIsNotNone(paths)
        backup_file, _meta = paths
        self.assertTrue(os.path.exists(backup_file),
                        "flush_autosave_all 应写出崩溃恢复备份")


class TestSaveConflict(_Base):
    """#3：保存前若磁盘已被改新（其它窗格/外部），先征求用户，默认不覆盖。"""

    def test_conflict_no_aborts_and_preserves_disk(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("my local edit\n")
        self._make_disk_newer(pane, p, "external wrote this\n")
        calls = []
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: (calls.append(1), QMessageBox.StandardButton.No)[1])
        try:
            self.assertFalse(pane.save_file())  # No → 中止保存
        finally:
            QMessageBox.question = orig
        self.assertTrue(calls, "应弹出保存冲突提示")
        with open(p) as f:
            self.assertEqual(f.read(), "external wrote this\n")  # 磁盘未被覆盖

    def test_conflict_yes_overwrites(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("force mine\n")
        self._make_disk_newer(pane, p, "external\n")
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Yes)
        try:
            self.assertTrue(pane.save_file())
        finally:
            QMessageBox.question = orig
        with open(p) as f:
            self.assertEqual(f.read(), "force mine\n")


class TestAutoSaveOnLeave(_Base):
    """失焦自动保存：静默落盘，且绝不弹任何对话框。

    自动保存发生在用户切走的瞬间，此时弹模态框比不保存更糟——所有需要
    提问/报错的分支都必须放弃保存、保持脏状态，留给手动保存处理。
    """

    def _no_dialogs(self):
        """上下文：把 QMessageBox 的问答/警告换成会让测试失败的哨兵。"""
        from PyQt6.QtWidgets import QMessageBox

        seen = []
        orig_q, orig_w = QMessageBox.question, QMessageBox.warning
        QMessageBox.question = staticmethod(
            lambda *a, **k: (seen.append('question'),
                             QMessageBox.StandardButton.No)[1])
        QMessageBox.warning = staticmethod(
            lambda *a, **k: seen.append('warning'))

        class _Ctx:
            def __enter__(_s):
                return seen

            def __exit__(_s, *exc):
                QMessageBox.question = orig_q
                QMessageBox.warning = orig_w
                return False
        return _Ctx()

    def test_dirty_file_saved_silently(self):
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("edited\n")
        with self._no_dialogs() as seen:
            self.assertTrue(area.auto_save_all_dirty() == 1)
        self.assertFalse(seen, f"自动保存不应弹对话框: {seen}")
        with open(p) as f:
            self.assertEqual(f.read(), "edited\n")
        self.assertFalse(pane.is_modified())

    def test_clean_file_is_noop(self):
        area, pane, p = self._area_with_file("v1\n")
        before = os.path.getmtime(p)
        self.assertEqual(area.auto_save_all_dirty(), 0)
        self.assertEqual(os.path.getmtime(p), before)

    def test_conflict_skipped_without_prompt(self):
        """磁盘副本更新时静默跳过：不弹框，也不覆盖别人的改动。"""
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("my local edit\n")
        self._make_disk_newer(pane, p, "external wrote this\n")
        with self._no_dialogs() as seen:
            self.assertEqual(area.auto_save_all_dirty(), 0)
        self.assertFalse(seen, f"冲突时自动保存不应弹对话框: {seen}")
        with open(p) as f:
            self.assertEqual(f.read(), "external wrote this\n")
        self.assertTrue(pane.is_modified(), "跳过保存后应保持脏状态")

    def test_write_failure_does_not_warn(self):
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("edited\n")
        with self._no_dialogs() as seen:
            with unittest.mock.patch.object(
                    pane, '_atomic_write_bytes',
                    side_effect=OSError("read-only fs")):
                self.assertEqual(area.auto_save_all_dirty(), 0)
        self.assertFalse(seen, f"写失败时自动保存不应弹警告: {seen}")
        self.assertTrue(pane.is_modified())

    def test_disabled_switch_blocks_auto_save(self):
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("edited\n")
        area.set_auto_save_enabled(False)
        self.assertEqual(area.auto_save_all_dirty(), 0)
        with open(p) as f:
            self.assertEqual(f.read(), "v1\n")   # 关掉后不写盘
        area.set_auto_save_enabled(True)
        self.assertEqual(area.auto_save_all_dirty(), 1)

    def test_untitled_buffer_never_opens_save_dialog(self):
        """未命名缓冲区：save_file 会退化成「另存为」文件选择框，必须跳过。"""
        from file_editor import EditorArea
        area = EditorArea(theme={})
        self._areas.append(area)
        pane = area.active_pane
        pane.editor.setPlainText("scratch\n")
        called = []
        with unittest.mock.patch.object(
                pane, 'save_file_as', side_effect=lambda: called.append(1)):
            self.assertEqual(area.auto_save_all_dirty(), 0)
        self.assertFalse(called, "未命名文件不应触发另存为对话框")


class TestAutoSaveNeverWipes(_Base):
    """数据丢失回归：自动保存绝不能把非空文件清空。

    真实事故：open_file 里 `_current_file` / `_original_content` 已指向新文件、
    而缓冲区还没填内容时，中途的 _maybe_restore_autosave 会弹模态对话框；
    对话框带来的焦点变化触发失焦自动保存，它看到「基准=新文件内容，
    缓冲区=空」判定为已修改，把空内容写进刚打开的文件 → 整个文件被清空。
    """

    def test_loading_window_does_not_wipe_file(self):
        """模拟装载中途（基准已换、缓冲区未填）触发自动保存。"""
        area, pane, p = self._area_with_file("REAL CONTENT\n")
        # 手工制造装载中途状态
        pane._loading = True
        pane.editor.setPlainText("")
        try:
            self.assertEqual(area.auto_save_all_dirty(), 0)
        finally:
            pane._loading = False
        with open(p) as f:
            self.assertEqual(f.read(), "REAL CONTENT\n", "文件被自动保存清空了")

    def test_empty_buffer_never_overwrites_nonempty_file(self):
        """兜底红线：即便没有 _loading 标记，空缓冲区也不得清空非空文件。"""
        area, pane, p = self._area_with_file("REAL CONTENT\n")
        pane.editor.setPlainText("")          # 缓冲区空，磁盘非空
        self.assertTrue(pane.is_modified())
        self.assertFalse(pane.auto_save_if_dirty(),
                         "自动保存应拒绝把非空文件清空")
        with open(p) as f:
            self.assertEqual(f.read(), "REAL CONTENT\n")

    def test_manual_save_can_still_empty_a_file(self):
        """手动保存仍应允许清空文件——这条红线只针对自动保存。"""
        area, pane, p = self._area_with_file("REAL CONTENT\n")
        pane.editor.setPlainText("")
        self.assertTrue(pane.save_file())     # 非 silent
        with open(p) as f:
            self.assertEqual(f.read(), "")

    def test_auto_save_during_open_file_dialog_is_blocked(self):
        """端到端：open_file 中途弹对话框时触发自动保存，文件必须完好。"""
        area, pane, p = self._area_with_file("ORIGINAL\n")
        other = os.path.join(os.path.dirname(p), "second.py")
        with open(other, "w") as f:
            f.write("SECOND FILE\n")

        seen = {}

        def _during(fp, disk):
            # _maybe_restore_autosave 会弹模态对话框；此刻缓冲区尚未填好，
            # 装载标记必须已经立起，否则焦点变化触发的自动保存会写坏文件
            seen['loading'] = pane._loading
            seen['buffer'] = pane.editor.toPlainText()
            seen['baseline'] = pane._original_content
            seen['saved'] = area.auto_save_all_dirty()
            return None
        pane._maybe_restore_autosave = _during

        self.assertTrue(area.open_file_in_active(other))
        # 前置：确实处在「基准已换、缓冲区还是旧内容」的危险窗口
        self.assertEqual(seen['baseline'], "SECOND FILE\n")
        self.assertNotEqual(seen['buffer'], seen['baseline'],
                            "前置失败：未处于装载中途状态")
        self.assertTrue(seen['loading'],
                        "装载中途必须置 _loading，否则自动保存会写坏文件")
        self.assertEqual(seen['saved'], 0,
                         "装载期间不应有任何窗格被自动保存")
        with open(other) as f:
            self.assertEqual(f.read(), "SECOND FILE\n", "新打开的文件被写坏了")
        self.assertEqual(pane.editor.toPlainText(), "SECOND FILE\n")

    def test_normal_auto_save_still_works(self):
        """红线不能误伤正常场景：有内容的改动仍要正常落盘。"""
        area, pane, p = self._area_with_file("v1\n")
        pane.editor.setPlainText("v2 edited\n")
        self.assertTrue(pane.auto_save_if_dirty())
        with open(p) as f:
            self.assertEqual(f.read(), "v2 edited\n")

    def test_empty_file_stays_saveable(self):
        """磁盘本来就是空文件时，自动保存不受红线影响。"""
        area, pane, p = self._area_with_file("")
        pane.editor.setPlainText("now has content\n")
        self.assertTrue(pane.auto_save_if_dirty())
        with open(p) as f:
            self.assertEqual(f.read(), "now has content\n")


class TestExternalChangeAfterSave(_Base):
    """#4：自己的保存靠字节指纹识别（不再用时间窗），保存后短时间内真实的外部
    改动不再被吞掉。"""

    def test_self_save_recognized_no_prompt(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("a\n")
        pane.editor.setPlainText("b\n")
        self.assertTrue(pane.save_file())
        # 保存后继续打字（缓冲区≠磁盘），再把 mtime 顶新以绕过 mtime 相等的早退，
        # 强制走到字节指纹比对那一步
        pane.editor.setPlainText("b kept typing\n")
        future = time.time() + 10
        os.utime(p, (future, future))
        calls = []
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: (calls.append(1), QMessageBox.StandardButton.No)[1])
        try:
            pane._handle_external_change()
        finally:
            QMessageBox.question = orig
        self.assertEqual(calls, [], "磁盘==自己刚写的字节 → 不应弹外部改动提示")
        self.assertEqual(pane.editor.toPlainText(), "b kept typing\n")  # 未被重载

    def test_real_external_change_detected(self):
        area, pane, p = self._area_with_file("a\n")
        # 干净状态（无本地改动）下外部写入不同内容 → 静默重载到新内容
        with open(p, "w") as f:
            f.write("external new\n")
        future = time.time() + 10
        os.utime(p, (future, future))
        pane._handle_external_change()
        self.assertEqual(pane.editor.toPlainText(), "external new\n")


class TestCRLFLineEndings(_Base):
    """CRLF（Windows 常见）文件：QPlainTextEdit 载入会把 \\r\\n 折成 \\n，若
    _original_content 仍保留 \\r\\n，文件一打开就被误判“已修改”，干净状态下的
    外部改动会错误地弹模态确认框——离屏/CI 里无人应答就卡死。"""

    def _area_with_crlf_file(self, content_lf="a\nb\n"):
        from file_editor import EditorArea
        d = tempfile.mkdtemp()
        p = os.path.join(d, "crlf.txt")
        # 直接写 CRLF 字节，跨平台稳定复现 Windows 行尾（不依赖文本模式翻译）
        with open(p, "wb") as f:
            f.write(content_lf.replace("\n", "\r\n").encode("utf-8"))
        area = EditorArea(theme={})
        self.assertTrue(area.open_file_in_active(p))
        self._areas.append(area)
        return area, area.active_pane, p

    def test_crlf_file_not_modified_on_load(self):
        area, pane, p = self._area_with_crlf_file("a\nb\n")
        # 行尾应归一为 \n，且不被判成已修改
        self.assertEqual(pane.editor.toPlainText(), "a\nb\n")
        self.assertFalse(pane.is_modified(),
                         "CRLF 文件刚载入不应被判为已修改")

    def test_crlf_external_change_reloads_without_prompt(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_crlf_file("a\nb\n")
        with open(p, "wb") as f:
            f.write(b"external\r\nnew\r\n")
        future = time.time() + 10
        os.utime(p, (future, future))
        # 即便误弹，也 patch 成不阻塞，并断言根本不该问
        calls = []
        orig = QMessageBox.question
        QMessageBox.question = staticmethod(
            lambda *a, **k: (calls.append(1), QMessageBox.StandardButton.No)[1])
        try:
            pane._handle_external_change()
        finally:
            QMessageBox.question = orig
        self.assertEqual(calls, [], "干净的 CRLF 文件遇外部改动应静默重载，不弹框")
        self.assertEqual(pane.editor.toPlainText(), "external\nnew\n")


class TestStaleAutosaveRestore(_Base):
    """#5：备份比磁盘旧时，恢复对话框默认「否」并用警示文案，避免顺手回车覆盖
    较新的磁盘内容。"""

    def test_disk_newer_defaults_to_no(self):
        from PyQt6.QtWidgets import QMessageBox
        area, pane, p = self._area_with_file("disk v1\n")
        # 制造一个比磁盘"旧"的崩溃恢复备份
        pane.editor.setPlainText("old unsaved\n")
        pane._autosave_tick()
        paths = pane._backup_paths()
        self.assertIsNotNone(paths)
        self.assertTrue(os.path.exists(paths[0]))
        # 磁盘文件随后被外部更新成更新的版本
        with open(p, "w") as f:
            f.write("disk v2 newer\n")
        future = time.time() + 100
        os.utime(p, (future, future))

        captured = {}
        orig = QMessageBox.question

        def fake_q(parent, title, text, buttons, default=None):
            captured["default"] = default
            captured["text"] = text
            return QMessageBox.StandardButton.No

        QMessageBox.question = staticmethod(fake_q)
        try:
            restored = pane._maybe_restore_autosave(p, "disk v2 newer\n")
        finally:
            QMessageBox.question = orig
        self.assertIsNone(restored)  # No → 不恢复
        self.assertEqual(captured.get("default"), QMessageBox.StandardButton.No,
                         "磁盘更新时默认按钮应为 No")


if __name__ == "__main__":
    unittest.main()
