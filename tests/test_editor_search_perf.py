"""编辑器搜索栏性能改造的行为守卫

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_editor_search_perf -v

覆盖：输入防抖（显式动作前 flush）、高亮条数封顶（计数不封顶）、
替换走匹配时的全文快照。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from file_editor import CodeEditor, _SearchBar
        self.editor = CodeEditor()
        self.bar = _SearchBar(self.editor, {})

    def tearDown(self):
        # 先停防抖定时器再销毁，避免已排队的 timeout 残留到后续测试的
        # processEvents 时投递（会崩整个 suite）
        self.bar._debounce.stop()
        self.bar.deleteLater()
        self.editor.deleteLater()
        self.app.processEvents()


class TestDebounce(_Base):
    def test_typing_defers_update(self):
        self.editor.setPlainText('abc abc abc')
        self.bar.input.setText('abc')
        # 输入只是启动防抖定时器，未立即重算
        self.assertEqual(self.bar._matches, [])
        self.assertTrue(self.bar._debounce.isActive())

    def test_goto_next_flushes_pending(self):
        self.editor.setPlainText('abc abc abc')
        self.bar.input.setText('abc')
        self.bar._goto_next()
        self.assertEqual(len(self.bar._matches), 3)
        self.assertGreaterEqual(self.bar._current, 0)
        self.assertFalse(self.bar._debounce.isActive())

    def test_explicit_update_cancels_pending(self):
        self.editor.setPlainText('abc')
        self.bar.input.setText('abc')
        self.bar._update_matches(reset_current=True)
        self.assertFalse(self.bar._debounce.isActive())

    def test_close_search_stops_timer(self):
        self.editor.setPlainText('abc')
        self.bar.input.setText('abc')
        self.bar.close_search()
        self.assertFalse(self.bar._debounce.isActive())


class TestHighlightCap(_Base):
    def test_selections_capped_but_count_full(self):
        n = self.bar._MAX_HIGHLIGHTS * 3
        self.editor.setPlainText('e' * n)
        self.bar.input.setText('e')
        self.bar._flush_pending()
        self.assertEqual(len(self.bar._matches), n)
        # 高亮对象数封顶，计数标签仍显示全量
        self.assertEqual(len(self.editor._search_selections),
                         self.bar._MAX_HIGHLIGHTS)
        self.assertIn(str(n), self.bar.count_label.text())

    def test_below_cap_all_highlighted(self):
        self.editor.setPlainText('x y x y x')
        self.bar.input.setText('x')
        self.bar._flush_pending()
        self.assertEqual(len(self.editor._search_selections), 3)

    def test_window_follows_current_match(self):
        cap = self.bar._MAX_HIGHLIGHTS
        self.editor.setPlainText('e' * (cap * 3))
        self.bar.input.setText('e')
        self.bar._flush_pending()
        # 把当前命中挪到尾部，窗口应跟过去（含当前命中）
        self.bar._current = cap * 3 - 1
        self.bar._refresh_highlights()
        sels = self.editor._search_selections
        self.assertEqual(len(sels), cap)
        last_pos = max(s.cursor.selectionStart() for s in sels)
        self.assertEqual(last_pos, cap * 3 - 1)


class TestReplaceSnapshot(_Base):
    def test_replace_all_uses_snapshot(self):
        self.editor.setPlainText('foo bar foo')
        self.bar.input.setText('foo')
        self.bar._flush_pending()
        self.bar.replace_input.setText('X')
        self.bar._replace_all()
        self.assertEqual(self.editor.toPlainText(), 'X bar X')

    def test_replace_one_regex_backreference(self):
        self.editor.setPlainText('ab ab')
        self.bar.regex_btn.setChecked(True)
        self.bar.input.setText(r'(a)(b)')
        self.bar._flush_pending()
        self.bar.replace_input.setText(r'\2\1')
        self.bar._replace_one()
        self.assertEqual(self.editor.toPlainText(), 'ba ab')


if __name__ == '__main__':
    unittest.main()
