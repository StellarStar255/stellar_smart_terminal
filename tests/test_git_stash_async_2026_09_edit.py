"""git 面板 stash 系列 / 分支删除前置检查 / 引用切换前置检查不能在 GUI 线程跑 git。

审查发现：commit / checkout 早已走后台 worker，但 _on_stash_save 直接
stash_save()、stash 对话框的 pop/apply/drop/list 直接同步调用，
_on_delete_branch 的 get_current_branch() 与 _on_ref_changed 的
get_head_ref() 也在 GUI 线程起子进程（撞上 index.lock 会睡满退避梯子）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_git_stash_async_2026_09_edit.py -v
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def _git(repo, *args):
    return subprocess.run(
        ['git', '-C', repo, *args],
        capture_output=True, text=True, check=True,
        env={**os.environ,
             'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
             'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'},
    ).stdout.strip()


class TestGitStashAsync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix='git_stash_test_')
        self.repo = self._tmp.name
        _git(self.repo, 'init', '-b', 'main')
        with open(os.path.join(self.repo, 'a.txt'), 'w') as f:
            f.write('hello\n')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-m', 'init')
        _git(self.repo, 'config', 'user.name', 't')
        _git(self.repo, 'config', 'user.email', 't@t')

        from git_widget import GitPanel
        self.panel = GitPanel()
        # 失败路径会经 error_occurred 弹模态 QMessageBox，offscreen 下永久阻塞
        try:
            self.panel._git_manager.error_occurred.disconnect()
        except TypeError:
            pass
        self.git_errors = []
        self.panel._git_manager.error_occurred.connect(self.git_errors.append)
        self.panel.set_repository(self.repo)
        self._wait_workers()
        self.gui_ident = threading.get_ident()

    def tearDown(self):
        from PyQt6.QtCore import QEvent
        self.panel.shutdown()
        try:
            self.panel._git_manager._stop_watching()
        except Exception:
            pass
        self.panel.deleteLater()
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self._tmp.cleanup()

    def _wait_workers(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            busy = (self.panel._commit_running or self.panel._checkout_running
                    or getattr(self.panel, '_stash_running', False)
                    or any(w.isRunning() for w in self.panel._active_workers))
            if not busy:
                self.app.processEvents()
                return
            time.sleep(0.01)
        raise TimeoutError("git workers did not finish")

    def _spy(self, obj, name):
        seen = []
        orig = getattr(obj, name)

        def wrapper(*a, **k):
            seen.append(threading.get_ident())
            return orig(*a, **k)
        return mock.patch.object(obj, name, side_effect=wrapper), seen

    def _dirty(self):
        with open(os.path.join(self.repo, 'a.txt'), 'a') as f:
            f.write('more\n')

    # ---- stash save ----
    def test_stash_save_runs_off_gui_thread(self):
        import git_widget
        self._dirty()
        patcher, seen = self._spy(self.panel._git_manager, 'stash_save')
        with patcher, mock.patch.object(git_widget.QInputDialog, 'getText',
                                        return_value=('wip', True)):
            self.panel._on_stash_save()
            self.assertTrue(self.panel._stash_running, "stash 没有进入后台忙碌态")
            self._wait_workers()
        self.assertTrue(seen, "stash_save 没被调用")
        self.assertNotIn(self.gui_ident, seen, "stash_save 在 GUI 线程里跑了")
        self.assertIn('wip', _git(self.repo, 'stash', 'list'),
                      msg=f"git errors: {self.git_errors}")
        self.assertFalse(self.panel._stash_running)

    def test_stash_save_nothing_to_save_informs(self):
        import git_widget
        with mock.patch.object(git_widget.QInputDialog, 'getText',
                               return_value=('', True)), \
                mock.patch.object(git_widget.QMessageBox, 'information') as info:
            self.panel._on_stash_save()
            self._wait_workers()
        self.assertTrue(info.called, "没有可贮藏的修改时应提示")

    def test_stash_save_reentrant_ignored(self):
        import git_widget
        self._dirty()
        with mock.patch.object(git_widget.QInputDialog, 'getText',
                               return_value=('one', True)):
            self.panel._on_stash_save()
            self.panel._on_stash_save()   # 忙碌中重复触发应被忽略
            self._wait_workers()
        self.assertEqual(len(_git(self.repo, 'stash', 'list').splitlines()), 1)

    # ---- stash dialog ----
    def test_stash_dialog_list_and_pop_off_gui_thread(self):
        from git_widget import _StashDialog
        self._dirty()
        _git(self.repo, 'stash', 'push', '-m', 'saved')
        gm = self.panel._git_manager
        list_patcher, list_seen = self._spy(gm, 'stash_list')
        pop_patcher, pop_seen = self._spy(gm, 'stash_pop')
        with list_patcher, pop_patcher:
            dlg = _StashDialog(self.panel)
            self._wait_workers()
            self.assertTrue(list_seen, "stash_list 没被调用")
            self.assertNotIn(self.gui_ident, list_seen, "stash_list 在 GUI 线程里跑了")
            self.assertEqual(dlg.list_widget.count(), 1)
            self.assertIn('saved', dlg.list_widget.item(0).text())
            self.assertTrue(dlg.pop_btn.isEnabled())

            dlg.list_widget.setCurrentRow(0)
            dlg.do_op('pop')
            self._wait_workers()
        self.assertTrue(pop_seen, "stash_pop 没被调用")
        self.assertNotIn(self.gui_ident, pop_seen, "stash_pop 在 GUI 线程里跑了")
        self.assertEqual(_git(self.repo, 'stash', 'list'), '',
                         msg=f"git errors: {self.git_errors}")
        # pop 完成后列表就地重载：只剩"空"占位，按钮禁用
        self.assertEqual(dlg.list_widget.count(), 1)
        self.assertFalse(dlg.pop_btn.isEnabled())
        with open(os.path.join(self.repo, 'a.txt')) as f:
            self.assertIn('more', f.read())

    def test_stash_dialog_drop_requires_confirmation(self):
        import git_widget
        from git_widget import _StashDialog
        self._dirty()
        _git(self.repo, 'stash', 'push', '-m', 'keep me')
        dlg = _StashDialog(self.panel)
        self._wait_workers()
        dlg.list_widget.setCurrentRow(0)
        with mock.patch.object(git_widget.QMessageBox, 'question',
                               return_value=git_widget.QMessageBox.StandardButton.No):
            dlg.do_op('drop')
            self._wait_workers()
        self.assertIn('keep me', _git(self.repo, 'stash', 'list'))

    # ---- delete branch / ref changed 的前置检查不再起子进程 ----
    def test_delete_current_branch_check_uses_cached_head(self):
        import git_widget
        gm = self.panel._git_manager
        with mock.patch.object(gm, 'get_current_branch',
                               side_effect=AssertionError('GUI 线程跑了 git')), \
                mock.patch.object(git_widget.QMessageBox, 'warning') as warn:
            self.panel._on_delete_branch('main')
            self._wait_workers()
        self.assertTrue(warn.called, "删除当前分支应被拒绝并提示")
        self.assertEqual(_git(self.repo, 'rev-parse', '--abbrev-ref', 'HEAD'), 'main')

    def test_ref_changed_same_ref_uses_cached_head(self):
        gm = self.panel._git_manager
        with mock.patch.object(gm, 'get_head_ref',
                               side_effect=AssertionError('GUI 线程跑了 git')):
            self.panel._on_ref_changed('local', 'main')
        self.assertFalse(self.panel._checkout_running)


if __name__ == '__main__':
    unittest.main()
