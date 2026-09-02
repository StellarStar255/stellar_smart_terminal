# -*- coding: utf-8 -*-
"""Git 批次回归测试：子进程 stdin/ssh 环境、GUI 线程不退避睡眠、
面板 git 调用异步化、diff 输出上限、git 缺失的友好提示。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_git_batch.py -v
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import git_manager as gm_mod
from git_manager import GitManager


def _git(repo, *args):
    return subprocess.run(
        ['git', '-C', repo, *args], capture_output=True, text=True, check=True,
        env={**os.environ,
             'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
             'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'},
    ).stdout.strip()


class _AppBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])


class _FakeProc:
    """替身 Popen：记录参数，立即返回空输出。"""
    calls = []

    def __init__(self, args, **kwargs):
        _FakeProc.calls.append((args, kwargs))
        self.pid = os.getpid()
        self.returncode = 0

    def communicate(self, input=None, timeout=None):
        return '', ''


class TestSpawnEnv(_AppBase):
    """ssh 的口令/host-key 提示会挂在启动本程序的 tty 上，直到 120s 超时。"""

    def setUp(self):
        self.gm = GitManager()
        self.gm._repo_path = tempfile.gettempdir()
        _FakeProc.calls.clear()

    def _spawn(self, *args, **kw):
        with mock.patch.object(gm_mod.subprocess, 'Popen', _FakeProc):
            self.gm._run_git(*args, **kw)
        self.assertEqual(len(_FakeProc.calls), 1)
        return _FakeProc.calls[0][1]

    def test_stdin_is_devnull_without_input(self):
        kw = self._spawn('status')
        self.assertEqual(kw['stdin'], subprocess.DEVNULL)

    def test_stdin_is_pipe_with_input(self):
        kw = self._spawn('apply', '-', input_text='patch')
        self.assertEqual(kw['stdin'], subprocess.PIPE)

    def test_batchmode_injected_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('GIT_SSH_COMMAND', None)
            os.environ.pop('GIT_SSH', None)
            kw = self._spawn('push')
        self.assertEqual(kw['env'].get('GIT_SSH_COMMAND'), 'ssh -oBatchMode=yes')
        self.assertEqual(kw['env'].get('GIT_TERMINAL_PROMPT'), '0')

    def test_user_ssh_command_wins(self):
        with mock.patch.dict(os.environ, {'GIT_SSH_COMMAND': 'ssh -i ~/.ssh/k'}):
            kw = self._spawn('push')
        self.assertEqual(kw['env']['GIT_SSH_COMMAND'], 'ssh -i ~/.ssh/k')

    def test_user_git_ssh_wins(self):
        with mock.patch.dict(os.environ, {'GIT_SSH': '/usr/bin/plink'}):
            os.environ.pop('GIT_SSH_COMMAND', None)
            kw = self._spawn('push')
        self.assertNotIn('GIT_SSH_COMMAND', kw['env'])


_LOCK_MSG = "fatal: Unable to create '/r/.git/index.lock': File exists."


class TestLockRetryThreadAware(_AppBase):
    """index.lock 退避重试的 sleep 只许在工作线程里跑。"""

    def setUp(self):
        self.gm = GitManager()
        self.gm._repo_path = tempfile.gettempdir()
        self.once_calls = 0

        def fake_once(args, check, timeout, input_text):
            self.once_calls += 1
            return False, _LOCK_MSG
        self.gm._run_git_once = fake_once

    def test_gui_thread_never_sleeps(self):
        def boom(_s):
            raise AssertionError("time.sleep on GUI thread")
        with mock.patch.object(gm_mod.time, 'sleep', boom):
            ok, out = self.gm._run_git('add', '--', 'x')
        self.assertFalse(ok)
        self.assertIn('index.lock', out)
        # 允许一次立即重试，但不能整梯子跑完
        self.assertLessEqual(self.once_calls, 2)

    def test_worker_thread_keeps_backoff(self):
        slept = []
        result = {}

        def run():
            with mock.patch.object(gm_mod.time, 'sleep', slept.append):
                result['r'] = self.gm._run_git('add', '--', 'x')
        th = threading.Thread(target=run)
        th.start()
        th.join(5)
        self.assertEqual(slept, list(gm_mod._LOCK_RETRY_DELAYS))
        self.assertEqual(self.once_calls, len(gm_mod._LOCK_RETRY_DELAYS) + 1)


class TestOutputCap(_AppBase):
    def setUp(self):
        self.gm = GitManager()
        self.gm._repo_path = tempfile.gettempdir()
        self.big = 'x' * (gm_mod.MAX_DIFF_CHARS + 500_000)
        self.gm._run_git = lambda *a, **k: (True, self.big)

    def test_get_diff_capped(self):
        out = self.gm.get_diff('a.txt')
        self.assertLess(len(out), len(self.big))
        self.assertLessEqual(len(out), gm_mod.MAX_DIFF_CHARS + 200)
        self.assertTrue(out.startswith('x' * 100))
        self.assertNotEqual(out[-1], 'x', "结尾应有截断标记")

    def test_commit_show_capped(self):
        out = self.gm.get_commit_show('abc')
        self.assertLessEqual(len(out), gm_mod.MAX_DIFF_CHARS + 200)
        self.assertNotEqual(out[-1], 'x')

    def test_small_output_untouched(self):
        self.gm._run_git = lambda *a, **k: (True, 'small diff')
        self.assertEqual(self.gm.get_diff('a.txt'), 'small diff')


class TestGitMissing(_AppBase):
    def test_set_repository_reports_friendly_error(self):
        tmp = tempfile.mkdtemp(prefix='gitmissing_')
        _git(tmp, 'init', '-b', 'main')
        gm = GitManager()
        errors = []
        gm.error_occurred.connect(errors.append)
        with mock.patch.object(gm_mod.shutil, 'which', lambda *_a, **_k: None):
            ok = gm.set_repository(tmp)
        self.assertFalse(ok)
        self.assertEqual(len(errors), 1)
        self.assertNotIn('Errno', errors[0])
        self.assertNotEqual(errors[0].strip(), '')


class TestPanelAsync(_AppBase):
    """面板里的 diff / show / stage 等不再在 GUI 线程同步跑 git。"""

    def setUp(self):
        from git_widget import GitPanel
        self.panel = GitPanel()
        try:
            self.panel._git_manager.error_occurred.disconnect()
        except TypeError:
            pass
        self.main_thread = threading.current_thread()

    def tearDown(self):
        self.panel.shutdown()
        self.panel.deleteLater()
        self.app.processEvents()

    def _wait(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if not any(w.isRunning() for w in self.panel._active_workers):
                self.app.processEvents()
                return
            time.sleep(0.01)
        raise TimeoutError("git workers did not finish")

    def test_show_diff_runs_off_gui_thread(self):
        seen = {}

        def fake_get_diff(path, staged=False):
            seen['thread'] = threading.current_thread()
            return 'DIFF-BODY'
        self.panel._git_manager.get_diff = fake_get_diff
        got = []
        self.panel.diff_requested.connect(
            lambda title, content, path, staged: got.append((title, content, path, staged)))

        self.panel._show_diff('a.txt', False)
        self._wait()
        self.assertIn('thread', seen)
        self.assertIsNot(seen['thread'], self.main_thread, "get_diff 仍在 GUI 线程同步执行")
        self.assertEqual(got, [('a.txt', 'DIFF-BODY', 'a.txt', False)])

    def test_commit_show_runs_off_gui_thread(self):
        seen = {}

        def fake_show(h):
            seen['thread'] = threading.current_thread()
            return 'SHOW'
        self.panel._git_manager.get_commit_show = fake_show
        got = []
        self.panel.output_requested.connect(lambda title, text: got.append(text))
        self.panel._on_commit_clicked('abcdef1234')
        self._wait()
        self.assertIsNot(seen.get('thread'), self.main_thread)
        self.assertEqual(got, ['SHOW'])

    def test_stage_file_runs_off_gui_thread(self):
        seen = {}

        def fake_stage(path):
            seen['thread'] = threading.current_thread()
            return True
        self.panel._git_manager.stage_file = fake_stage
        self.panel.changes_widget.stage_file.emit('a.txt')
        self._wait()
        self.assertIn('thread', seen, "stage_file 信号没有走到（可能仍直连旧的绑定方法）")
        self.assertIsNot(seen['thread'], self.main_thread, "stage_file 仍在 GUI 线程")


if __name__ == '__main__':
    unittest.main()
