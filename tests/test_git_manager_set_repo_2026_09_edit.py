"""GitManager.set_repository 重复设同一仓库不能再起子进程。

审查发现：每次切标签都会走 shutil.which('git') + `git rev-parse
--show-toplevel`（GUI 线程同步），"同一仓库不重设"的短路却排在
rev-parse 之后，等于每次切标签白跑一次 git。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_git_manager_set_repo_2026_09_edit.py -v
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def _init_repo(path):
    subprocess.run(['git', '-C', path, 'init', '-b', 'main'],
                   check=True, capture_output=True)


class TestSetRepositoryShortCircuit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='git_setrepo_')
        self.repo = os.path.realpath(self._tmp.name)
        _init_repo(self.repo)
        os.makedirs(os.path.join(self.repo, 'sub', 'deep'))
        from git_manager import GitManager
        self.gm = GitManager()

    def tearDown(self):
        from PyQt6.QtCore import QEvent
        self.gm._stop_watching()
        self.gm.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self._tmp.cleanup()

    def _count_subprocesses(self, fn):
        import git_manager
        with mock.patch.object(git_manager.subprocess, 'run',
                               wraps=git_manager.subprocess.run) as run, \
                mock.patch.object(git_manager.subprocess, 'Popen',
                                  wraps=git_manager.subprocess.Popen) as popen, \
                mock.patch.object(git_manager.shutil, 'which',
                                  wraps=git_manager.shutil.which) as which:
            result = fn()
        return result, run.call_count + popen.call_count, which.call_count

    def test_second_set_same_repo_spawns_nothing(self):
        self.assertTrue(self.gm.set_repository(self.repo))
        ok, procs, whiches = self._count_subprocesses(
            lambda: self.gm.set_repository(self.repo))
        self.assertTrue(ok)
        self.assertEqual(procs, 0, f"重复 set_repository 起了 {procs} 个子进程")
        self.assertEqual(whiches, 0, "重复 set_repository 又 which 了一次 git")

    def test_subdir_of_same_repo_spawns_nothing(self):
        self.assertTrue(self.gm.set_repository(self.repo))
        ok, procs, _ = self._count_subprocesses(
            lambda: self.gm.set_repository(os.path.join(self.repo, 'sub', 'deep')))
        self.assertTrue(ok)
        self.assertEqual(procs, 0)
        self.assertEqual(self.gm._repo_path, self.repo)

    def test_switching_repo_still_detects(self):
        self.assertTrue(self.gm.set_repository(self.repo))
        with tempfile.TemporaryDirectory(prefix='git_setrepo2_') as other:
            other = os.path.realpath(other)
            _init_repo(other)
            self.assertTrue(self.gm.set_repository(other))
            self.assertEqual(self.gm._repo_path, other)
            self.gm._stop_watching()
        # 非仓库 → False，之后回到原仓库仍能正确识别
        with tempfile.TemporaryDirectory(prefix='not_repo_') as plain:
            self.assertFalse(self.gm.set_repository(plain))
        self.assertTrue(self.gm.set_repository(self.repo))
        self.assertEqual(self.gm._repo_path, self.repo)


if __name__ == '__main__':
    unittest.main()
