"""历史对话框性能改造守卫

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_history_dialog_perf.py -v

覆盖：预览文本片段截断（strip_ansi 不整份处理超长条目）、
详情文本单条/总量截断标记、列表与预览的后台加载 + latest-wins。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from session_manager import Session, SessionEntry, SessionManager
import history_dialog as hd


def _entry(content, type='output', files=None):
    return SessionEntry(type=type, content=content, timestamp='t',
                        files=files or [])


def _session(entries):
    return Session(session_id='s1', start_time='t0', command='cmd',
                   entries=entries)


class TestPreviewBuilder(unittest.TestCase):
    def test_huge_entry_snippet_capped(self):
        s = _session([_entry('x' * 1_000_000)])
        text = hd._build_preview_text(s)
        # 输出规模与条目长度无关（只取片段），且带省略标记
        self.assertLess(len(text), 2000)
        self.assertIn('...', text)

    def test_entry_count_capped_with_marker(self):
        s = _session([_entry(f'entry-{i}') for i in range(25)])
        text = hd._build_preview_text(s)
        self.assertIn('entry-19', text)
        self.assertNotIn('entry-20', text)
        self.assertIn('5', text.splitlines()[-1])  # more_entries n=5

    def test_short_entry_untouched(self):
        s = _session([_entry('short output')])
        text = hd._build_preview_text(s)
        self.assertIn('short output', text)
        self.assertNotIn('short output...', text)


class TestDetailBuilder(unittest.TestCase):
    def test_oversized_entry_truncated_with_marker(self):
        omitted = 12345
        s = _session([_entry('a' * (hd._DETAIL_ENTRY_CAP + omitted))])
        text = hd._build_detail_text(s)
        self.assertLess(len(text), hd._DETAIL_ENTRY_CAP + 2000)
        self.assertIn(str(omitted), text)  # entry_truncated n=omitted

    def test_total_cap_stops_and_marks_remaining(self):
        n_entries = 30
        per = hd._DETAIL_TOTAL_CAP // 25  # 25 条到达总量上限
        s = _session([_entry('a' * per) for _ in range(n_entries)])
        text = hd._build_detail_text(s)
        self.assertEqual(text.count('─── OUTPUT ───'), 25)
        self.assertIn('5', text.splitlines()[-1])  # entries_omitted n=5

    def test_small_session_complete(self):
        s = _session([_entry('one'), _entry('two', type='input',
                                            files=['f.txt'])])
        text = hd._build_detail_text(s)
        self.assertIn('one', text)
        self.assertIn('two', text)
        self.assertIn('f.txt', text)
        self.assertIn('═══ INPUT ═══', text)


class TestHistoryDialogAsync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, cond, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def test_list_and_preview_load_in_background(self):
        tmp = tempfile.mkdtemp()
        manager = SessionManager()
        manager.sessions_dir = Path(tmp)
        for sid in ('s1', 's2'):
            data = {'session_id': sid, 'start_time': 't0', 'command': 'cmd',
                    'entries': [{'type': 'output', 'content': f'hello-{sid}',
                                 'timestamp': 't'}]}
            (Path(tmp) / f'{sid}.json').write_text(
                json.dumps(data), encoding='utf-8')

        dlg = hd.HistoryDialog(manager)
        try:
            # 列表后台加载完成
            self.assertTrue(self._wait(lambda: dlg.table.rowCount() == 2))
            # 选中一行：查看/导出先禁用，预览后台加载完成后启用
            dlg.table.selectRow(0)
            self.assertFalse(dlg.view_btn.isEnabled())
            self.assertTrue(self._wait(lambda: dlg.current_session is not None))
            self.assertTrue(dlg.view_btn.isEnabled())
            self.assertTrue(dlg.export_btn.isEnabled())
            sid = dlg.current_session.session_id
            self.assertIn(f'hello-{sid}', dlg.preview.toPlainText())
        finally:
            dlg.deleteLater()
            self.app.processEvents()
            manager._save_executor.shutdown(wait=True)

    def test_stale_preview_result_discarded(self):
        """加载结果到达时选中已变 → 丢弃（latest-wins）"""
        tmp = tempfile.mkdtemp()
        manager = SessionManager()
        manager.sessions_dir = Path(tmp)
        data = {'session_id': 's1', 'start_time': 't0', 'command': 'cmd',
                'entries': []}
        (Path(tmp) / 's1.json').write_text(json.dumps(data), encoding='utf-8')

        dlg = hd.HistoryDialog(manager)
        try:
            self.assertTrue(self._wait(lambda: dlg.table.rowCount() == 1))
            dlg.table.selectRow(0)
            self.assertTrue(self._wait(lambda: dlg.current_session is not None))
            # 伪造一个过期回调：session_id 与当前选中不符
            stale = Session(session_id='ghost', start_time='t', command='c')
            dlg._on_preview_loaded('ghost', stale, 'stale text')
            self.assertEqual(dlg.current_session.session_id, 's1')
            self.assertNotIn('stale text', dlg.preview.toPlainText())
        finally:
            dlg.deleteLater()
            self.app.processEvents()
            manager._save_executor.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
