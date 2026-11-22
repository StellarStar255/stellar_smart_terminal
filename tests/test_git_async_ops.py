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
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

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
        from PyQt6.QtCore import QEvent
        self.panel.shutdown()
        # 先把 GitManager 的文件监视器和 5s 轮询停掉，再删仓库目录。否则
        # 旧面板还盯着一个正在消失的 .git（kqueue 事件 + 又起 git 子进程），
        # 而 processEvents() 并不处理 deleteLater —— 上一个用例的面板会一直
        # 活到模块结束，几个用例叠起来就是 macOS runner 上那个随机段错误。
        try:
            self.panel._git_manager._stop_watching()
        except Exception:
            pass  # 已经停过或对象已销毁
        self.panel.deleteLater()
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)   # 真删掉
        self.app.processEvents()
        self._tmp.cleanup()

    def _wait_workers(self, timeout=10.0):
        """跑事件循环直到所有后台 worker 完成（含 done 信号派发）"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            busy = (self.panel._commit_running or self.panel._checkout_running
                    or self.panel._message_generation is not None
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

    def test_generate_rejects_reasoning_only_then_fills_commit_message(self):
        Path(self.repo, 'a.txt').write_text('ready\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        config = {'api_base': 'https://api.minimax.io/v1/chat/completions',
                  'model': 'MiniMax-M3', 'max_tokens': 4096}
        responses = []
        for content, finish_reason in (
            ('<think>unfinished reasoning', 'length'),
            (None, 'stop'),
            ('<think>analysis</think>\n\x60\x60\x60\nfix: update readiness\n\x60\x60\x60', 'stop'),
        ):
            response = Mock(status_code=200)
            response.json.return_value = {'choices': [{
                'message': {'content': content}, 'finish_reason': finish_reason,
            }]}
            responses.append(response)
        widget = self.panel.commit_widget
        widget.message_input.setPlainText('keep the draft')
        with (patch.object(self.panel, '_find_main_window',
                           return_value=SimpleNamespace(get_git_llm_config=lambda: config)),
              patch('git_widget.QMessageBox.warning') as warning,
              patch('requests.post', side_effect=responses) as post):
            for count in (1, 2):
                widget.generate_btn.click()
                self._wait_workers()
                self.assertEqual(warning.call_count, count)
                self.assertEqual(widget.message_input.toPlainText(), 'keep the draft')
                self.assertTrue(widget.generate_btn.isEnabled())
            widget.generate_btn.click()
            self._wait_workers()
            self.assertEqual(warning.call_count, 2)
            self.assertEqual(widget.message_input.toPlainText(), 'fix: update readiness')
            self.assertTrue(widget.generate_btn.isEnabled())
            self.assertEqual(post.call_args.args[0], config['api_base'])
            payload = post.call_args.kwargs['json']
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            self.assertTrue(payload['reasoning_split'])
            self.assertEqual(payload['max_completion_tokens'], 1024)
            self.assertNotIn('max_tokens', payload)
            self.assertIn('+ready', payload['messages'][1]['content'])

    def test_generate_preserves_literal_thinking_tags_in_commit_messages(self):
        from git_widget import _CommitMessageWorker

        subject = 'fix(parser): preserve <think> and </think> literals'
        body = 'docs: show the reasoning tags\n\nThe literal <thinking> markup stays in the output.'
        examples = (
            (subject, subject),
            ('  <THINK>private analysis</THINK>\n<thinking>more</thinking>\n' + subject, subject),
            ('analysis without opening marker\n</think>\n' + subject, subject),
            (body, body),
            ('<thinking>only partial reasoning', ''),
            ('<think>only reasoning</think>', ''),
            ('analysis without answer\n</think>', ''),
        )
        for raw, expected in examples:
            with self.subTest(raw=raw):
                self.assertEqual(_CommitMessageWorker._clean(raw), expected)

    def test_generate_bounds_large_diff_and_detects_change_beyond_prompt(self):
        path = Path(self.repo, 'a.txt')
        prefix = ''.join(f'第 {i:04d} 行文本\n' for i in range(2300))
        path.write_text(prefix + 'original tail\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://example.test/v1', 'model': 'test'})
        widget = self.panel.commit_widget
        widget.message_input.setPlainText('keep existing draft')
        response = Mock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': 'fix: old result'}}]}

        def respond(*_args, **_kwargs):
            path.write_text(prefix + 'updated tail\n', encoding='utf-8')
            _git(self.repo, 'add', 'a.txt')
            return response

        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch('git_widget.QMessageBox.information') as info,
              patch('requests.post', side_effect=respond) as post):
            widget.generate_btn.click()
            self._wait_workers()
            text = post.call_args.kwargs['json']['messages'][1]['content'].split('\n\n', 1)[1]
            trimmed, marker = text.rsplit('\n', 1)
            self.assertEqual(len(trimmed), 16000)
            self.assertEqual(marker, '...[truncated]...')
            self.assertNotIn('original tail', trimmed)
            info.assert_called_once()
            self.assertEqual(widget.message_input.toPlainText(), 'keep existing draft')

    def test_generate_shows_provider_errors_returned_with_http_200(self):
        Path(self.repo, 'a.txt').write_text('ready\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://api.minimax.io/v1', 'model': 'MiniMax-M3'})
        widget = self.panel.commit_widget
        widget.message_input.setPlainText('existing draft')
        response = Mock(status_code=200)
        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch('git_widget.QMessageBox.warning') as warning,
              patch('requests.post', return_value=response)):
            for body, code in (
                ({'base_resp': {'status_code': 1002, 'status_msg': 'rate limit'}}, '1002'),
                ({'error': {'code': 'insufficient_quota', 'message': 'quota exhausted'}},
                 'insufficient_quota'),
            ):
                response.json.return_value = body
                widget.generate_btn.click()
                self._wait_workers()
                self.assertIn(code, warning.call_args.args[2])
                self.assertEqual(widget.message_input.toPlainText(), 'existing draft')
                self.assertTrue(widget.generate_btn.isEnabled())
            self.assertEqual(warning.call_count, 2)

    def test_generate_keeps_gateway_query_parameters_on_chat_endpoint(self):
        from git_widget import _CommitMessageWorker

        response = Mock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': 'fix: sample'}}]}
        expected = 'https://example.test/gateway/v1/chat/completions?api-version=1&tenant=a%2Fb'
        with patch('requests.post', return_value=response) as request:
            for url in (
                ' https://example.test/gateway/v1/?api-version=1&tenant=a%2Fb ',
                expected,
            ):
                worker = _CommitMessageWorker({'api_base': url, 'model': ' chat-model '}, 'diff', 'en')
                completed = Mock()
                worker.succeeded.connect(completed)
                worker.run()
                self.assertEqual(request.call_args.args[0], expected)
                self.assertEqual(request.call_args.kwargs['json']['model'], 'chat-model')
                completed.assert_called_once_with('fix: sample')

    def test_language_refresh_keeps_generate_button_busy(self):
        from i18n import get_language, set_language, t

        self.addCleanup(set_language, get_language())
        widget = self.panel.commit_widget
        widget.set_generating(True)
        set_language('fr')
        self.panel.apply_language()
        self.assertFalse(widget.generate_btn.isEnabled())
        self.assertEqual(widget.generate_btn.text(), t('git.generating'))
        widget.set_generating(False)
        self.assertEqual(widget.generate_btn.text(), t('git.generate_msg'))

    def test_generate_reports_invalid_gateway_settings_without_request(self):
        from i18n import t

        Path(self.repo, 'a.txt').write_text('ready\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        profile = {'api_base': '   ', 'model': 'test'}
        host = SimpleNamespace(get_git_llm_config=lambda: profile.copy())
        widget = self.panel.commit_widget
        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch('git_widget.QMessageBox.warning') as warning,
              patch('requests.post') as post):
            widget.generate_btn.click()
            self.assertEqual(warning.call_args.args[1], t('git.generate_no_config_title'))
            profile['api_base'] = True
            widget.generate_btn.click()
            self.assertEqual(warning.call_args.args[1], t('git.generate_no_config_title'))
            profile['api_base'] = 'example.test/v1'
            widget.generate_btn.click()
            self._wait_workers()
            self.assertIn(t('git.generate_invalid_url'), warning.call_args.args[2])
            self.assertEqual(warning.call_count, 3)
            post.assert_not_called()
            self.assertTrue(widget.generate_btn.isEnabled())

    def test_generate_masks_credentials_in_http_and_connection_errors(self):
        import requests
        from git_widget import _CommitMessageWorker
        from i18n import t

        config = {
            'api_base': 'https://gateway.test/v1?token=example-query%2Fsecret',
            'api_key': 'example-private-api-key',
            'model': 'test',
            'proxy': 'http://user:example-proxy-secret@proxy.test:8080',
            'timeout': 7,
        }
        response = Mock(status_code=429)
        response.json.return_value = {'error': {'code': 'rate_limit',
            'message': 'Rejected example-private-api-key and example-query/secret '
                       'via http://user:example-proxy-secret@proxy.test:8080'}}
        raw = Mock(status_code=401, text='x' * 290 + 'example-private-api-key')
        raw.json.side_effect = ValueError
        received = Mock()
        worker = _CommitMessageWorker(config, 'diff', 'en')
        worker.failed.connect(received)
        with patch('requests.post', side_effect=(
            response,
            requests.exceptions.ProxyError('proxy rejected ' + config['api_base']),
            requests.exceptions.ReadTimeout('timed out ' + config['api_base']),
            raw,
        )):
            for _ in range(4):
                worker.run()
        errors = [call.args[0] for call in received.call_args_list]
        self.assertIn('HTTP 429: rate_limit', errors[0])
        self.assertIn('ProxyError', errors[1])
        self.assertEqual(errors[2], t('git.generate_timeout', seconds=7))
        self.assertTrue(errors[3].startswith('HTTP 401: '))
        self.assertIn('[redacted]', errors[3])
        for secret in ('example-private', 'example-proxy-secret',
                       'example-query/secret', 'example-query%2Fsecret'):
            self.assertNotIn(secret, "\n".join(errors))

    def test_generate_discards_network_error_after_panel_shutdown(self):
        import requests
        from git_widget import _CommitMessageWorker

        Path(self.repo, 'a.txt').write_text('ready\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://example.test/v1', 'model': 'test'})
        widget = self.panel.commit_widget
        widget.message_input.setPlainText('keep this draft')
        started = Event()
        release = Event()

        def post(*_args, **_kwargs):
            started.set()
            release.wait(5)
            raise requests.exceptions.ConnectionError('connection lost after shutdown')

        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch.object(self.panel, '_SHUTDOWN_WAIT_MS', 20),
              patch('git_widget.QMessageBox.warning') as warning,
              patch('requests.post', side_effect=post)):
            widget.generate_btn.click()
            try:
                deadline = time.monotonic() + 4
                while not started.is_set() and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.01)
                self.assertTrue(started.is_set())
                worker = next(w for w in self.panel._active_workers
                              if isinstance(w, _CommitMessageWorker))
                self.panel.shutdown()
                self.assertIsNone(worker.parent())
            finally:
                release.set()
            self.assertTrue(worker.wait(5000))
            self.app.processEvents()
            warning.assert_not_called()
            self.assertEqual(widget.message_input.toPlainText(), 'keep this draft')

    def test_generate_rejects_filtered_and_nonfinal_model_responses(self):
        Path(self.repo, 'a.txt').write_text('ready\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://example.test/v1', 'model': 'test'})
        widget = self.panel.commit_widget
        response = Mock(status_code=200)
        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch('git_widget.QMessageBox.warning') as warning,
              patch('requests.post', return_value=response)):
            for count, (reason, fields) in enumerate((
                ('content_filter', {}),
                ('stop', {'refusal': 'I cannot provide the rest'}),
                ('tool_calls', {}),
                ('function_call', {}),
            ), 1):
                widget.message_input.setPlainText('keep the draft')
                response.json.return_value = {'choices': [{
                    'finish_reason': reason,
                    'message': {'content': 'fix: partial text', **fields},
                }]}
                widget.generate_btn.click()
                self._wait_workers()
                self.assertEqual(warning.call_count, count)
                self.assertEqual(widget.message_input.toPlainText(), 'keep the draft')
                self.assertTrue(widget.generate_btn.isEnabled())

    def test_generate_waits_for_merge_conflicts_to_be_resolved(self):
        from i18n import t

        path = Path(self.repo, 'a.txt')
        _git(self.repo, 'checkout', '-b', 'other')
        path.write_text('other branch\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        _git(self.repo, 'commit', '-m', 'other branch')
        _git(self.repo, 'checkout', 'main')
        path.write_text('main branch\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        _git(self.repo, 'commit', '-m', 'main branch')
        with self.assertRaises(subprocess.CalledProcessError):
            _git(self.repo, '-c', 'rerere.enabled=false', 'merge', '--no-edit', 'other')
        self.assertEqual(_git(self.repo, 'diff', '--name-only', '--diff-filter=U'), 'a.txt')

        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://example.test/v1', 'model': 'test'})
        widget = self.panel.commit_widget
        widget.message_input.setPlainText('keep the merge draft')
        response = Mock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': 'fix: resolve conflict'}}]}
        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch('git_widget.QMessageBox.warning') as warning,
              patch('requests.post', return_value=response) as post):
            widget.generate_btn.click()
            self._wait_workers()
            post.assert_not_called()
            self.assertIn(t('git.generate_unresolved_conflicts', n=1), warning.call_args.args[2])
            self.assertEqual(widget.message_input.toPlainText(), 'keep the merge draft')
            path.write_text('resolved change\n', encoding='utf-8')
            _git(self.repo, 'add', 'a.txt')
            widget.generate_btn.click()
            self._wait_workers()
            post.assert_called_once()
            self.assertIn('+resolved change', post.call_args.kwargs['json']['messages'][1]['content'])
            self.assertEqual(widget.message_input.toPlainText(), 'fix: resolve conflict')

    def test_generate_handles_empty_repo_and_first_staged_commit(self):
        fresh = Path(self.repo, 'fresh')
        fresh.mkdir()
        _git(str(fresh), 'init', '-b', 'main')
        self.assertEqual(self.panel._collect_diff_for_message(str(fresh)), ('', ''))
        (fresh / 'first.txt').write_text('first commit\n', encoding='utf-8')
        _git(str(fresh), 'add', 'first.txt')
        diff, error = self.panel._collect_diff_for_message(str(fresh))
        self.assertEqual(error, '')
        self.assertIn('+first commit', diff)

    def test_generate_checks_staged_content_again_before_filling(self):
        from i18n import t

        path = Path(self.repo, 'a.txt')
        path.write_text('first index version\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://example.test/v1', 'model': 'test'})
        widget = self.panel.commit_widget
        widget.message_input.setPlainText('existing draft')

        def post(*args, **kwargs):
            prompt = kwargs['json']['messages'][1]['content']
            if 'first index version' in prompt:
                path.write_text('new index version\n', encoding='utf-8')
                _git(self.repo, 'add', 'a.txt')
                message = 'fix: old result'
            else:
                path.write_text('working tree only\n', encoding='utf-8')
                message = 'fix: current index'
            response = Mock(status_code=200)
            response.json.return_value = {'choices': [{'message': {'content': message}}]}
            return response

        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch('git_widget.QMessageBox.information') as info,
              patch('requests.post', side_effect=post) as request):
            widget.generate_btn.click()
            self._wait_workers()
            self.assertEqual(info.call_args.args[1], t('git.generate_changes_updated_title'))
            self.assertEqual(widget.message_input.toPlainText(), 'existing draft')
            self.assertTrue(widget.generate_btn.isEnabled())
            widget.generate_btn.click()
            self._wait_workers()
            self.assertIn('new index version', request.call_args.kwargs['json']['messages'][1]['content'])
            self.assertEqual(info.call_count, 1)
            self.assertEqual(widget.message_input.toPlainText(), 'fix: current index')

    def test_generate_reads_only_staged_changes_and_uses_worktree_if_empty(self):
        root = Path(self.repo)
        (root / 'a.txt').write_text('staged edit\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        (root / 'a.txt').write_text('worktree edit\n', encoding='utf-8')
        (root / 'untracked-note.txt').write_text('untracked text\n', encoding='utf-8')
        diff, error = self.panel._collect_diff_for_message(self.repo)
        self.assertEqual(error, '')
        self.assertIn('staged edit', diff)
        self.assertNotIn('worktree edit', diff)
        self.assertNotIn('untracked-note.txt', diff)

        _git(self.repo, 'reset', 'HEAD', '--', 'a.txt')
        diff, error = self.panel._collect_diff_for_message(self.repo)
        self.assertEqual(error, '')
        self.assertIn('worktree edit', diff)
        self.assertIn('untracked-note.txt', diff)

    def test_generate_distinguishes_git_read_error_from_no_changes(self):
        from i18n import t

        missing = str(Path(self.repo, 'missing'))
        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://example.test/v1', 'model': 'test'})
        collect = self.panel._collect_diff_for_message
        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch.object(self.panel, '_collect_diff_for_message',
                           side_effect=lambda _: collect(missing)),
              patch('git_widget.QMessageBox.warning') as warning,
              patch('git_widget.QMessageBox.information') as info,
              patch('requests.post') as post):
            self.panel.commit_widget.generate_btn.click()
            self._wait_workers()
            warning.assert_called_once()
            self.assertIn(t('git_mgr.repo_path_missing', path=missing), warning.call_args.args[2])
            info.assert_not_called()
            post.assert_not_called()
            self.assertTrue(self.panel.commit_widget.generate_btn.isEnabled())

    def test_generate_preserves_draft_edited_while_request_is_running(self):
        Path(self.repo, 'a.txt').write_text('staged edit\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://example.test/v1', 'model': 'test'})
        widget = self.panel.commit_widget
        started = Event()
        release = Event()
        response = Mock(status_code=200)
        response.json.return_value = {
            'choices': [{'message': {'content': 'fix: generated'}}]}

        def post(*args, **kwargs):
            started.set()
            release.wait(5)
            return response

        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch('requests.post', side_effect=post)):
            widget.generate_btn.click()
            deadline = time.monotonic() + 4
            try:
                while not started.is_set() and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.01)
                self.assertTrue(started.is_set())
                widget.message_input.setPlainText('my edited draft')
            finally:
                release.set()
            self._wait_workers()
        self.assertTrue(widget.generate_btn.isEnabled())
        self.assertEqual(widget.message_input.toPlainText(), 'my edited draft')

    def test_generate_discards_result_after_switching_repositories(self):
        Path(self.repo, 'a.txt').write_text('repo a changes\n', encoding='utf-8')
        _git(self.repo, 'add', 'a.txt')
        repo_b = Path(self.repo, 'nested-repo')
        repo_b.mkdir()
        _git(str(repo_b), 'init', '-b', 'main')
        Path(repo_b, 'b.txt').write_text('repo b changes\n', encoding='utf-8')
        _git(str(repo_b), 'add', 'b.txt')
        host = SimpleNamespace(get_git_llm_config=lambda: {
            'api_base': 'https://example.test/v1', 'model': 'test'})
        widget = self.panel.commit_widget
        started = Event()
        release = Event()
        prompts = []

        def post(*args, **kwargs):
            prompt = kwargs['json']['messages'][1]['content']
            prompts.append(prompt)
            if 'repo a changes' in prompt:
                started.set()
                release.wait(5)
                message = 'fix: old repository'
            else:
                message = 'fix: active repository'
            response = Mock(status_code=200)
            response.json.return_value = {'choices': [{'message': {'content': message}}]}
            return response

        with (patch.object(self.panel, '_find_main_window', return_value=host),
              patch('requests.post', side_effect=post)):
            widget.generate_btn.click()
            deadline = time.monotonic() + 4
            try:
                while not started.is_set() and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(.01)
                self.assertTrue(started.is_set())
                self.panel.set_repository(str(repo_b))
                self.assertTrue(widget.generate_btn.isEnabled())
                widget.generate_btn.click()
            finally:
                release.set()
            self._wait_workers()
        self.assertEqual(len(prompts), 2)
        self.assertIn('repo b changes', prompts[1])
        self.assertNotIn('repo a changes', prompts[1])
        self.assertEqual(widget.message_input.toPlainText(), 'fix: active repository')
        diff, error = self.panel._collect_diff_for_message(self.repo)
        self.assertEqual(error, '')
        self.assertIn('repo a changes', diff)
        self.assertNotIn('repo b changes', diff)

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
