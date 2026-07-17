"""git 面板 commit / checkout 异步化测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_git_async_ops.py -v

commit 与 checkout 原来在 GUI 线程同步执行（_run_git 超时上限 30s，
pre-commit hook / 大仓库下会冻结整个窗口），现改为复用 _GitOpWorker
后台执行。这里用真实临时仓库端到端验证异步路径的行为与结果。
"""
import os
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git(repo, *args):
    return subprocess.run(
        ['git', '-C', repo, *args],
        capture_output=True, text=True, check=True,
        env={**os.environ,
             'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
             'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'},
    ).stdout.strip()


class TestGitAsyncOps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix='git_async_test_')
        self.repo = self._tmp.name
        _git(self.repo, 'init', '-b', 'main')
        with open(os.path.join(self.repo, 'a.txt'), 'w') as f:
            f.write('hello\n')
        _git(self.repo, 'add', '.')
        _git(self.repo, 'commit', '-m', 'init')
        # 面板的 commit 走 GitManager 自己 spawn 的 git，不带 _git() 那套身份
        # 环境变量。CI runner（Linux/Windows）没有全局 git 身份、gecos 又是
        # 空的，git 自动探测邮箱失败会以 "Author identity unknown" 拒绝提交
        # （macOS runner 因 gecos 有全名而侥幸通过）→ 仓库级配置身份自洽
        _git(self.repo, 'config', 'user.name', 't')
        _git(self.repo, 'config', 'user.email', 't@t')

        from git_widget import GitPanel
        self.panel = GitPanel()
        # 失败路径会经 error_occurred 弹模态 QMessageBox，offscreen 下会永久阻塞；
        # 换成记到列表——断言失败时能看到底层 git 报错，而不是只有结果不符
        try:
            self.panel._git_manager.error_occurred.disconnect()
        except TypeError:
            pass
        self.git_errors = []
        self.panel._git_manager.error_occurred.connect(self.git_errors.append)
        self.panel.set_repository(self.repo)
        self._wait_workers()  # set_repository 触发的初始刷新先跑完

    def tearDown(self):
        self.panel.shutdown()
        self.panel.deleteLater()
        self.app.processEvents()
        self._tmp.cleanup()

    def _wait_workers(self, timeout=10.0):
        """跑事件循环直到所有后台 worker 完成（含 done 信号派发）"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            busy = (self.panel._commit_running or self.panel._checkout_running
                    or any(w.isRunning() for w in self.panel._active_workers))
            if not busy:
                # 再抽一次事件队列，确保排队中的 done 回调执行完
                self.app.processEvents()
                return
            time.sleep(0.01)
        raise TimeoutError("git workers did not finish")

    def test_commit_runs_in_background_and_succeeds(self):
        with open(os.path.join(self.repo, 'b.txt'), 'w') as f:
            f.write('new\n')
        _git(self.repo, 'add', 'b.txt')
        self.panel.commit_widget.message_input.setPlainText('feat: async commit')

        self.panel._on_commit('feat: async commit')
        # 调用立即返回且处于忙碌态（工作在后台线程）
        self.assertTrue(self.panel._commit_running)
        self.assertFalse(self.panel.commit_widget.commit_btn.isEnabled())

        self._wait_workers()
        self.assertEqual(_git(self.repo, 'log', '-1', '--format=%s'),
                         'feat: async commit',
                         msg=f"git errors: {self.git_errors}")
        # 成功后：清空输入框、按钮恢复
        self.assertEqual(self.panel.commit_widget.message_input.toPlainText(), '')
        self.assertTrue(self.panel.commit_widget.commit_btn.isEnabled())
        self.assertEqual(self.panel.commit_widget.commit_btn.text(), 'Commit')

    def test_failed_commit_keeps_message(self):
        # 无暂存改动 → git commit 失败
        self.panel.commit_widget.message_input.setPlainText('will fail')
        self.panel._on_commit('will fail')
        self._wait_workers()
        # 失败时不清空用户写的提交信息，按钮恢复可用
        self.assertEqual(self.panel.commit_widget.message_input.toPlainText(),
                         'will fail')
        self.assertTrue(self.panel.commit_widget.commit_btn.isEnabled())

    def test_reentrant_commit_ignored(self):
        with open(os.path.join(self.repo, 'c.txt'), 'w') as f:
            f.write('x\n')
        _git(self.repo, 'add', 'c.txt')
        self.panel._on_commit('first')
        self.panel._on_commit('second')  # 忙碌中重复触发应被忽略
        self._wait_workers()
        self.assertEqual(_git(self.repo, 'log', '-1', '--format=%s'), 'first',
                         msg=f"git errors: {self.git_errors}")
        # 只产生了一个新提交（init + first）
        self.assertEqual(_git(self.repo, 'rev-list', '--count', 'HEAD'), '2')

    def test_checkout_runs_in_background(self):
        _git(self.repo, 'branch', 'feature')
        self.panel._on_ref_changed('local', 'feature')
        self.assertTrue(self.panel._checkout_running)

        self._wait_workers()
        self.assertEqual(_git(self.repo, 'rev-parse', '--abbrev-ref', 'HEAD'),
                         'feature')
        self.assertFalse(self.panel._checkout_running)

    def test_checkout_same_ref_is_noop(self):
        self.panel._on_ref_changed('local', 'main')  # 已在 main 上
        self.assertFalse(self.panel._checkout_running)


if __name__ == '__main__':
    unittest.main()
