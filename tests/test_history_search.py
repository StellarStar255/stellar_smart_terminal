# -*- coding: utf-8 -*-
"""跨会话历史全局搜索的测试

覆盖 SessionManager.search_sessions：
- 大小写不敏感子串命中、片段截取压单行
- ANSI 转义剥离后匹配（颜色码切碎关键词不漏检）
- 每会话命中上限 / 全局上限 / 取消
- 新会话优先（文件名倒序）
以及 HistoryDialog 搜索模式的表格切换与 _row_session_id 解析。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_history_search.py -v
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session_manager import SessionManager


def _write_session(dirpath, sid, entries, command='claude'):
    data = {
        'session_id': sid,
        'start_time': '2026-07-28 10:00:00',
        'command': command,
        'entries': [
            {'type': typ, 'content': content, 'timestamp': 't'}
            for typ, content in entries
        ],
    }
    (Path(dirpath) / f'{sid}.json').write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')


class TestSearchSessions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='hist_search_')
        self.sm = SessionManager()
        self.sm.sessions_dir = Path(self._tmp.name)

    def tearDown(self):
        self.sm._save_executor.shutdown(wait=True)
        self._tmp.cleanup()

    def test_basic_hit_and_snippet(self):
        _write_session(self._tmp.name, 's1', [
            ('input', 'sed -i s/old/new/ file.txt'),
            ('output', 'done\nall replaced OK'),
        ])
        hits = self.sm.search_sessions('SED -I')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['session_id'], 's1')
        self.assertEqual(hits[0]['entry_type'], 'input')
        self.assertIn('sed -i', hits[0]['snippet'])

    def test_ansi_stripped_before_match(self):
        # 颜色码把关键词切碎：原始串搜不到，strip 后必须命中
        _write_session(self._tmp.name, 's2', [
            ('output', 'run \x1b[32mtriton\x1b[0mserver now'),
        ])
        self.assertEqual(len(self.sm.search_sessions('tritonserver')), 1)

    def test_snippet_is_single_line(self):
        _write_session(self._tmp.name, 's3', [
            ('output', 'aaa\nbbb needle ccc\nddd'),
        ])
        hits = self.sm.search_sessions('needle')
        self.assertNotIn('\n', hits[0]['snippet'])

    def test_per_session_and_global_caps(self):
        _write_session(self._tmp.name, 's4', [
            ('output', f'needle {i}') for i in range(10)
        ])
        hits = self.sm.search_sessions('needle', per_session_limit=3)
        self.assertEqual(len(hits), 3)
        for i in range(5):
            _write_session(self._tmp.name, f'zz{i}', [('output', 'needle')])
        hits = self.sm.search_sessions('needle', max_results=4)
        self.assertEqual(len(hits), 4)

    def test_newest_session_first(self):
        _write_session(self._tmp.name, 'a_old', [('output', 'needle old')])
        _write_session(self._tmp.name, 'z_new', [('output', 'needle new')])
        hits = self.sm.search_sessions('needle')
        self.assertEqual(hits[0]['session_id'], 'z_new')

    def test_cancel_and_empty_query(self):
        _write_session(self._tmp.name, 's5', [('output', 'needle')])
        self.assertEqual(self.sm.search_sessions(''), [])
        self.assertEqual(
            self.sm.search_sessions('needle', cancel_check=lambda: True), [])


class TestHistoryDialogSearchMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from history_dialog import HistoryDialog
        self._tmp = tempfile.TemporaryDirectory(prefix='hist_dialog_')
        self.sm = SessionManager()
        self.sm.sessions_dir = Path(self._tmp.name)
        _write_session(self._tmp.name, 'sess1', [('input', 'echo needle')])
        self.dlg = HistoryDialog(self.sm)
        # 等后台列表加载完成
        for _ in range(100):
            self.app.processEvents()
            if self.dlg._list_worker is None:
                break

    def tearDown(self):
        self.dlg.close()
        self.dlg.deleteLater()
        self.app.processEvents()
        self.sm._save_executor.shutdown(wait=True)
        self._tmp.cleanup()

    def test_search_mode_switch_and_row_session_id(self):
        dlg = self.dlg
        results = self.sm.search_sessions('needle')
        dlg.search_input.setText('needle')
        dlg._search_seq += 1
        dlg._on_search_done(dlg._search_seq, results)
        self.assertTrue(dlg._search_mode)
        self.assertEqual(dlg.table.columnCount(), 3)
        self.assertEqual(dlg.table.rowCount(), 1)
        # 第 0 列显示时间，但 _row_session_id 仍解析出真实会话 id
        self.assertEqual(dlg._row_session_id(0), 'sess1')
        # 清空搜索 → 恢复 5 列会话列表
        dlg.search_input.setText('')
        self.assertFalse(dlg._search_mode)
        self.assertEqual(dlg.table.columnCount(), 5)
        self.assertEqual(dlg._row_session_id(0), 'sess1')

    def test_stale_results_dropped(self):
        dlg = self.dlg
        dlg.search_input.setText('needle')
        stale_seq = dlg._search_seq
        dlg._search_seq += 1  # 模拟已发起了更新的搜索
        dlg._on_search_done(stale_seq, [{'session_id': 'x', 'command': 'c',
                                         'start_time': 't', 'entry_type': 'o',
                                         'snippet': 's'}])
        self.assertFalse(dlg._search_mode)


if __name__ == '__main__':
    unittest.main()
