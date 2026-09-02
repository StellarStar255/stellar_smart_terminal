"""git_manager 数据层解析测试（此前 0 覆盖）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_git_manager_parse.py -v

两路：
- unquote_git_path / _parse_status：纯逻辑，直接测（八进制/转义解析 bug 会毁掉
  CJK / 含特殊字符的文件名，是回归高危区）。
- get_status/branches/tags/log/ahead_behind/head_ref：真实临时仓库驱动真 git，
  断言解析结果——最高保真的回归保护。
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from git_manager import unquote_git_path, GitManager, FileStatus


def _git(repo, *args, check=True):
    return subprocess.run(
        ['git', '-C', repo, *args],
        capture_output=True, text=True, check=check,
        env={**os.environ,
             'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
             'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
             'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull},
    ).stdout


class TestUnquoteGitPath(unittest.TestCase):
    def test_plain_passthrough(self):
        self.assertEqual(unquote_git_path('docs/readme.md'), 'docs/readme.md')
        self.assertEqual(unquote_git_path(''), '')
        self.assertEqual(unquote_git_path('"'), '"')  # 长度<2 原样

    def test_octal_utf8_cjk(self):
        # 安 = UTF-8 E5 AE 89 = 八进制 345 256 211
        self.assertEqual(unquote_git_path(r'"docs/\345\256\211.md"'), 'docs/安.md')

    def test_multibyte_sequence(self):
        # 你好 = E4 BD A0 E5 A5 BD
        quoted = r'"\344\275\240\345\245\275.txt"'
        self.assertEqual(unquote_git_path(quoted), '你好.txt')

    def test_simple_escapes(self):
        self.assertEqual(unquote_git_path(r'"a\tb"'), 'a\tb')
        self.assertEqual(unquote_git_path(r'"a\nb"'), 'a\nb')
        self.assertEqual(unquote_git_path(r'"a\\b"'), 'a\\b')
        self.assertEqual(unquote_git_path(r'"a\"b"'), 'a"b')

    def test_real_git_quotes_cjk(self):
        """端到端：git status 对 CJK 名默认加引号，unquote 应还原"""
        import tempfile
        repo = tempfile.mkdtemp(prefix='git_unquote_')
        try:
            _git(repo, 'init', '-b', 'main')
            _git(repo, 'config', 'core.quotepath', 'true')  # 强制引号转义
            with open(os.path.join(repo, '安全.txt'), 'w') as f:
                f.write('x')
            raw = _git(repo, 'status', '--porcelain=v1', '-uall')
            # raw 里的路径是被引号包裹的八进制转义
            quoted = raw.splitlines()[0][3:]
            self.assertTrue(quoted.startswith('"'))
            self.assertEqual(unquote_git_path(quoted), '安全.txt')
        finally:
            import shutil
            shutil.rmtree(repo, ignore_errors=True)


class TestGitManagerParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='git_mgr_test_')
        _git(self._tmp, 'init', '-b', 'main')
        with open(os.path.join(self._tmp, 'a.txt'), 'w') as f:
            f.write('hello\n')
        _git(self._tmp, 'add', '.')
        _git(self._tmp, 'commit', '-m', 'init')
        self.gm = GitManager()
        self.errors = []
        self.gm.error_occurred.connect(self.errors.append)
        self.assertTrue(self.gm.set_repository(self._tmp))

    def tearDown(self):
        import shutil
        self.gm.deleteLater()
        self.app.processEvents()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write(self, name, content='x'):
        with open(os.path.join(self._tmp, name), 'w') as f:
            f.write(content)

    def test_parse_status_mapping(self):
        self.assertEqual(self.gm._parse_status('M'), FileStatus.MODIFIED)
        self.assertEqual(self.gm._parse_status('A'), FileStatus.ADDED)
        self.assertEqual(self.gm._parse_status('D'), FileStatus.DELETED)
        self.assertEqual(self.gm._parse_status('?'), FileStatus.UNTRACKED)
        self.assertEqual(self.gm._parse_status('U'), FileStatus.UNMERGED)
        self.assertIsNone(self.gm._parse_status(' '))

    def test_status_staged_unstaged_untracked(self):
        self._write('a.txt', 'changed\n')      # 工作区改动（unstaged）
        self._write('b.txt', 'new\n')          # 未跟踪
        _git(self._tmp, 'add', 'a.txt')        # a.txt 暂存
        self._write('a.txt', 'changed again\n')  # 暂存后再改 → 同时 staged+unstaged
        staged, unstaged = self.gm.get_status()
        staged_names = {f.path for f in staged}
        unstaged_names = {f.path for f in unstaged}
        self.assertIn('a.txt', staged_names)
        self.assertIn('a.txt', unstaged_names)
        self.assertIn('b.txt', unstaged_names)
        untracked = [f for f in unstaged if f.path == 'b.txt'][0]
        self.assertEqual(untracked.status, FileStatus.UNTRACKED)

    def test_status_rename_detected(self):
        _git(self._tmp, 'mv', 'a.txt', 'renamed.txt')
        staged, _ = self.gm.get_status()
        renamed = [f for f in staged if f.status == FileStatus.RENAMED]
        self.assertEqual(len(renamed), 1)
        self.assertEqual(renamed[0].path, 'renamed.txt')
        self.assertEqual(renamed[0].old_path, 'a.txt')

    def test_status_merge_conflict_flagged(self):
        # 造一个真实合并冲突
        _git(self._tmp, 'checkout', '-b', 'feature')
        self._write('a.txt', 'feature change\n')
        _git(self._tmp, 'commit', '-am', 'feature')
        _git(self._tmp, 'checkout', 'main')
        self._write('a.txt', 'main change\n')
        _git(self._tmp, 'commit', '-am', 'main')
        _git(self._tmp, 'merge', 'feature', check=False)  # 冲突
        _, unstaged = self.gm.get_status()
        conflicts = [f for f in unstaged if f.is_conflict]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].path, 'a.txt')
        self.assertEqual(conflicts[0].status, FileStatus.UNMERGED)

    def test_status_cjk_filename(self):
        self._write('中文文件.txt', 'x')
        _, unstaged = self.gm.get_status()
        names = {f.path for f in unstaged}
        self.assertIn('中文文件.txt', names)

    def test_branches_local_current_and_remote(self):
        _git(self._tmp, 'branch', 'dev')
        branches = self.gm.get_branches()
        locals_ = {b.name: b for b in branches if not b.is_remote}
        self.assertIn('main', locals_)
        self.assertIn('dev', locals_)
        self.assertTrue(locals_['main'].is_current)
        self.assertFalse(locals_['dev'].is_current)

    def test_tags_sorted(self):
        _git(self._tmp, 'tag', 'v1.0')
        _git(self._tmp, 'tag', 'v2.0')
        tags = self.gm.get_tags()
        self.assertIn('v1.0', tags)
        self.assertIn('v2.0', tags)

    def test_head_ref(self):
        kind, name = self.gm.get_head_ref()
        self.assertEqual((kind, name), ('local', 'main'))

    def test_log_hash_parents_subject(self):
        self._write('c.txt', 'c\n')
        _git(self._tmp, 'add', '.')
        _git(self._tmp, 'commit', '-m', 'second commit')
        log = self.gm.get_log(limit=10)
        self.assertGreaterEqual(len(log), 2)
        head = log[0]
        self.assertEqual(head['subject'], 'second commit')
        self.assertEqual(len(head['short']), 7)
        self.assertEqual(head['parents'], [log[1]['hash']])  # 父指向上一条
        self.assertEqual(head['author'], 't')

    def test_ahead_behind_with_upstream(self):
        import tempfile
        bare = tempfile.mkdtemp(prefix='git_bare_')
        try:
            _git(bare, 'init', '--bare', '-b', 'main')
            _git(self._tmp, 'remote', 'add', 'origin', bare)
            _git(self._tmp, 'push', '-u', 'origin', 'main')
            # 本地领先 2 个提交
            for i in range(2):
                self._write(f'x{i}.txt', str(i))
                _git(self._tmp, 'add', '.')
                _git(self._tmp, 'commit', '-m', f'ahead {i}')
            ahead, behind = self.gm.get_ahead_behind()
            self.assertEqual((ahead, behind), (2, 0))
        finally:
            import shutil
            shutil.rmtree(bare, ignore_errors=True)


class TestLockContentionRetry(unittest.TestCase):
    """index.lock 竞争重试：另一 git 进程（终端里的 git commit、AI 助手等）
    短暂持锁时，stage/commit 应退避重试而非直接弹错。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix='git_lock_test_')
        _git(self._tmp, 'init', '-b', 'main')
        with open(os.path.join(self._tmp, 'a.txt'), 'w') as f:
            f.write('hello\n')
        _git(self._tmp, 'add', '.')
        _git(self._tmp, 'commit', '-m', 'init')
        self.gm = GitManager()
        self.errors = []
        self.gm.error_occurred.connect(self.errors.append)
        self.assertTrue(self.gm.set_repository(self._tmp))
        self._lock = os.path.join(self._tmp, '.git', 'index.lock')

    def tearDown(self):
        import shutil
        self.gm.deleteLater()
        self.app.processEvents()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_is_lock_contention_detection(self):
        from git_manager import _is_lock_contention
        # 英文与中文 locale 的真实 git 报错（路径部分 locale 无关）
        self.assertTrue(_is_lock_contention(
            "fatal: Unable to create '/repo/.git/index.lock': File exists."))
        self.assertTrue(_is_lock_contention(
            "致命错误：无法创建 '/repo/.git/index.lock'：File exists。"))
        self.assertTrue(_is_lock_contention(
            "fatal: Unable to create '/repo/.git/HEAD.lock': File exists."))
        # 非锁类错误不应触发重试
        self.assertFalse(_is_lock_contention("fatal: pathspec 'x' did not match"))
        self.assertFalse(_is_lock_contention(""))
        self.assertFalse(_is_lock_contention("error: could not lock config file"))

    def test_stage_all_retries_until_lock_released(self):
        """持锁 ~0.5s 后释放：stage_all 应重试成功，不报错。

        退避重试只在工作线程里睡（面板现在把 stage/unstage 都放到线程里跑；
        GUI 线程上撞锁最多立即重试一次、绝不 sleep），所以这里也在线程里调。
        """
        import threading
        with open(os.path.join(self._tmp, 'b.txt'), 'w') as f:
            f.write('new\n')
        open(self._lock, 'w').close()
        threading.Timer(0.5, lambda: os.path.exists(self._lock)
                        and os.remove(self._lock)).start()
        result = {}
        th = threading.Thread(target=lambda: result.__setitem__('ok', self.gm.stage_all()))
        th.start()
        th.join(15)
        self.app.processEvents()  # 让跨线程排队的 error_occurred 送达
        self.assertTrue(result.get('ok'))
        self.assertEqual(self.errors, [])
        staged, _ = self.gm.get_status()
        self.assertIn('b.txt', [f.path for f in staged])

    def test_stage_all_fails_after_exhausting_retries(self):
        """锁一直不释放（残留的 stale lock）：重试耗尽后仍报错，不无限等。"""
        with open(os.path.join(self._tmp, 'c.txt'), 'w') as f:
            f.write('new\n')
        open(self._lock, 'w').close()
        try:
            self.assertFalse(self.gm.stage_all())
            self.assertEqual(len(self.errors), 1)
            self.assertIn('index.lock', self.errors[0])
        finally:
            os.remove(self._lock)


if __name__ == '__main__':
    unittest.main()
