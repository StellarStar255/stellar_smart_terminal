"""Remote search: real server script, channel lifecycle, and incremental Qt UI."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from unittest import mock

from ssh_session import SSHSession, RemoteEntry, _REMOTE_SEARCH_SCRIPT
from tests.test_remote_explorer_logic import _Base


class TestServerScan(unittest.TestCase):
    def scan(self, root, tokens, hidden=False, limit=500, dirs=600, depth=6, seconds=15):
        result = subprocess.run(
            [sys.executable, '-u', '-c', _REMOTE_SEARCH_SCRIPT,
             json.dumps([str(root), tokens, hidden, limit, dirs, depth, seconds])],
            capture_output=True, check=True, timeout=5)
        messages = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(messages[0]['ready'])
        self.assertTrue(messages[-1]['done'])
        return [e for m in messages for e in m.get('entries', [])], messages[-1]

    @unittest.skipIf(os.name == 'nt', 'POSIX filenames and symlinks require a POSIX filesystem')
    def test_literals_unicode_hidden_symlinks_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'child').mkdir()
            name = 'GROUPED [*] 中文\nfile.txt'
            (root / 'child' / name).write_text('hello')
            (root / '.hidden').mkdir()
            (root / '.hidden' / name).touch()
            (root / 'link').symlink_to(root / 'child', target_is_directory=True)
            entries, final = self.scan(root, ['grouped', '[*]', '中文'])
            self.assertEqual([e['name'] for e in entries], [name])
            self.assertEqual(entries[0]['size'], 5)
            self.assertFalse(final['truncated'])
            self.assertEqual(len(self.scan(root, ['grouped'], hidden=True)[0]), 2)
            entries, _ = self.scan(root, ['link'])
            self.assertTrue(entries[0]['is_link'])
            self.assertFalse(entries[0]['is_dir'])

    def test_limits_report_incomplete_even_without_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'child').mkdir()
            (root / 'child' / 'grouped.txt').touch()
            for limits in ({'dirs': 1}, {'depth': 0}, {'seconds': 0}):
                entries, final = self.scan(root, ['grouped'], **limits)
                self.assertEqual(entries, [])
                self.assertTrue(final['truncated'])
            (root / 'grouped1').touch()
            (root / 'grouped2').touch()
            entries, final = self.scan(root, ['grouped'], limit=1)
            self.assertEqual(len(entries), 1)
            self.assertTrue(final['truncated'])


class Channel:
    def __init__(self, output=b''):
        # Deliberately split JSON/UTF-8 records across SSH packets.
        self.chunks = [output[i:i + 7] for i in range(0, len(output), 7)]
        self.closed = False

    def settimeout(self, seconds):
        pass

    def exec_command(self, command):
        self.command = command

    def recv_stderr_ready(self):
        return False

    def recv_ready(self):
        return bool(self.chunks)

    def recv(self, size):
        return self.chunks.pop(0)

    def exit_status_ready(self):
        return not self.chunks

    def close(self):
        self.closed = True


class TestSearchChannel(unittest.TestCase):
    def run_search(self, channel, callback, stop=lambda: False, root='/x'):
        session = mock.Mock()
        session._client.get_transport.return_value.open_session.return_value = channel
        SSHSession.search_names(session, root, ['grouped'], False, 500, 600, 6,
                                callback, stop)
        session.submit.assert_not_called()
        session._client.get_transport.return_value.open_session.assert_called_once_with(timeout=3)
        return session

    def test_stream_and_quote_arguments_without_sftp(self):
        entry = dict(name='grouped.txt', path='/x/grouped.txt', is_dir=False)
        channel = Channel(('\n'.join(json.dumps(m) for m in [
            {'ready': True}, {'entries': [entry]},
            {'entries': [], 'done': True, 'truncated': False}]) + '\n').encode())
        calls = []
        root = "/tmp/a'; $(touch nope) 中文"
        self.run_search(channel, lambda *args: calls.append(args), root=root)
        self.assertEqual(calls[0][0][0].name, 'grouped.txt')
        self.assertFalse(calls[0][2])
        self.assertTrue(calls[-1][2])
        self.assertEqual(json.loads(shlex.split(channel.command)[-1])[0], root)
        self.assertTrue(channel.closed)

    def test_unavailable_python_falls_back_and_closes(self):
        channel = Channel()
        with self.assertRaises(NotImplementedError):
            self.run_search(channel, mock.Mock())
        self.assertTrue(channel.closed)

    def test_cancel_closes_channel_during_exec_request(self):
        cancelled = threading.Event()
        channel = Channel()
        def stalled(command):
            cancelled.set()
            deadline = time.monotonic() + 2
            while not channel.closed and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(channel.closed)
            raise RuntimeError('channel closed')
        channel.exec_command = stalled
        callback = mock.Mock()
        self.run_search(channel, callback, cancelled.is_set)
        callback.assert_not_called()
        self.assertTrue(channel.closed)

    def test_missing_final_record_is_an_error(self):
        channel = Channel(b'{"ready": true}\n')
        with self.assertRaises(RuntimeError):
            self.run_search(channel, mock.Mock())
        self.assertTrue(channel.closed)

    def test_deadline_returns_partial_results_as_incomplete(self):
        channel = Channel(b'{"ready": true}\n')
        channel.chunks = [b'{"ready": true}\n']
        now = [0.0]
        original_recv = channel.recv
        def recv(size):
            data = original_recv(size)
            now[0] = 21.0
            return data
        channel.recv = recv
        calls = []
        with mock.patch('ssh_session.time.monotonic', lambda: now[0]):
            self.run_search(channel, lambda *args: calls.append(args))
        self.assertEqual(calls, [([], True, True)])
        self.assertTrue(channel.closed)

    def test_actual_server_script_scans_many_directories_in_one_channel(self):
        channel = Channel()
        def execute(command):
            args = shlex.split(command)
            args[0] = sys.executable  # Simulate the server's Python, including on Windows.
            result = subprocess.run(args, capture_output=True,
                                    check=True, timeout=5)
            channel.chunks = [result.stdout]
        channel.exec_command = execute
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(150):
                child = root / str(index)
                child.mkdir()
                (child / 'grouped.txt').touch()
            calls = []
            self.run_search(channel, lambda *args: calls.append(args), root=str(root))
            self.assertEqual(sum(len(args[0]) for args in calls), 150)
            self.assertTrue(calls[-1][2])
            self.assertFalse(calls[-1][1])
            self.assertFalse(calls[0][2])


class TestSearchPanel(_Base):
    def pump_until(self, condition):
        deadline = time.monotonic() + 2
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(condition())

    def panel(self, session):
        panel = self._panel()
        self.addCleanup(panel.close)
        panel._session = session
        panel._current_path = '/x'
        panel._search_edit.setText('grouped')
        panel._search_timer.stop()
        return panel

    def test_partial_results_visible_while_remote_scan_runs_and_stale_ignored(self):
        release = threading.Event()
        ended = threading.Event()
        self.addCleanup(release.set)
        session = mock.Mock()
        entry = RemoteEntry('grouped', '/x/grouped', False)
        def search(*args):
            batch = args[6]
            batch([entry], False, False)
            release.wait(2)
            batch([RemoteEntry('old', '/x/old', False)], False, True)
            ended.set()
        session.search_names.side_effect = search
        panel = self.panel(session)
        panel._start_search()
        self.pump_until(lambda: panel._search_results.count() == 2)
        self.assertIn('grouped', panel._search_results.item(0).text())
        self.assertFalse(ended.is_set())
        session.submit.assert_not_called()
        panel._search_edit.clear()
        release.set()
        self.pump_until(ended.is_set)
        self.app.processEvents()
        self.assertEqual(panel._search_results.count(), 0)

    def test_fallback_streams_and_cancelled_queued_task_does_no_io(self):
        session = mock.Mock()
        session.search_names.side_effect = NotImplementedError
        pending = []
        def submit(fn):
            future = Future()
            pending.append((fn, future))
            return future
        session.submit.side_effect = submit
        panel = self.panel(session)
        panel._start_search()
        self.pump_until(lambda: len(pending) == 1)
        session.listdir.return_value = [RemoteEntry('grouped', '/x/grouped', False),
                                      RemoteEntry('child', '/x/child', True)]
        fn, future = pending.pop(0)
        future.set_result(fn())
        self.pump_until(lambda: len(pending) == 1)
        self.assertIn('grouped', panel._search_results.item(0).text())
        panel._search_edit.clear()
        session.listdir.reset_mock()
        fn, future = pending.pop(0)
        future.set_result(fn())
        self.app.processEvents()
        session.listdir.assert_not_called()
        self.assertEqual(panel._search_results.count(), 0)

    def test_empty_incomplete_is_not_reported_as_no_matches(self):
        from i18n import t
        panel = self.panel(mock.Mock())
        panel._on_search_results(panel._search_gen, [], True)
        self.assertIn(t('search.incomplete', count=0), panel._search_results.item(0).text())

    def test_failed_fallback_submission_finishes_search(self):
        from i18n import t
        session = mock.Mock()
        session.search_names.side_effect = NotImplementedError
        session.submit.side_effect = RuntimeError('executor closed')
        panel = self.panel(session)
        panel._start_search()
        self.pump_until(lambda: panel._search_results.item(0).text() ==
                        t('search.incomplete', count=0))

    def test_close_invalidates_search_and_stops_debounce(self):
        panel = self.panel(mock.Mock())
        old_gen = panel._search_gen
        panel._search_timer.start()
        panel.close()
        self.assertGreater(panel._search_gen, old_gen)
        self.assertFalse(panel._search_timer.isActive())


if __name__ == '__main__':
    unittest.main()
