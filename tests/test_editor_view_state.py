"""编辑器视图位置记忆测试：切换文件后再切回，恢复离开时的光标与滚动位置。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_editor_view_state -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import file_editor
        file_editor._VIEW_STATE_REGISTRY.clear()
        self._areas = []

    def tearDown(self):
        # 与 test_editor_save_safety 同款清理：断开 watcher，防残留信号
        for area in self._areas:
            for pane in list(getattr(area, '_panes', [])):
                fw = getattr(pane, '_file_watcher', None)
                if fw is not None:
                    try:
                        files = fw.files()
                        if files:
                            fw.removePaths(files)
                        fw.fileChanged.disconnect()
                    except (TypeError, RuntimeError):
                        pass
            try:
                area.deleteLater()
            except Exception:
                pass
        self._areas = []
        self.app.processEvents()

    def _make_area(self):
        from file_editor import EditorArea
        area = EditorArea(theme={})
        area.resize(800, 600)
        area.show()
        self._areas.append(area)
        return area

    def _write_file(self, name, lines=300):
        d = getattr(self, '_dir', None) or tempfile.mkdtemp()
        self._dir = d
        p = os.path.join(d, name)
        with open(p, "w") as f:
            f.write("\n".join(f"line {i}" for i in range(lines)) + "\n")
        return p


class TestViewStateRestore(_Base):
    def test_switch_back_restores_cursor_and_scroll(self):
        area = self._make_area()
        pa = self._write_file("a.txt")
        pb = self._write_file("b.txt")
        self.assertTrue(area.open_file_in_active(pa))
        pane = area.active_pane
        self.app.processEvents()

        # 光标移到 100 行附近并滚动
        pane.goto_line(101)
        self.app.processEvents()
        cursor_pos = pane.editor.textCursor().position()
        scroll_val = pane.editor.verticalScrollBar().value()
        self.assertGreater(scroll_val, 0)

        # 切到 b 再切回 a
        self.assertTrue(area.open_file_in_active(pb))
        self.app.processEvents()
        self.assertEqual(pane.editor.verticalScrollBar().value(), 0)
        self.assertTrue(area.open_file_in_active(pa))
        self.app.processEvents()

        self.assertEqual(pane.editor.textCursor().position(), cursor_pos)
        self.assertEqual(pane.editor.verticalScrollBar().value(), scroll_val)

    def test_first_open_starts_at_top(self):
        area = self._make_area()
        pa = self._write_file("a.txt")
        self.assertTrue(area.open_file_in_active(pa))
        pane = area.active_pane
        self.app.processEvents()
        self.assertEqual(pane.editor.textCursor().position(), 0)
        self.assertEqual(pane.editor.verticalScrollBar().value(), 0)

    def test_cursor_clamped_when_file_shrinks(self):
        area = self._make_area()
        pa = self._write_file("a.txt", lines=300)
        pb = self._write_file("b.txt")
        self.assertTrue(area.open_file_in_active(pa))
        pane = area.active_pane
        pane.goto_line(280)
        self.assertTrue(area.open_file_in_active(pb))

        # 外部把 a 改短，切回时光标应钳制在文档范围内
        with open(pa, "w") as f:
            f.write("short\n")
        self.assertTrue(area.open_file_in_active(pa))
        self.app.processEvents()
        pos = pane.editor.textCursor().position()
        self.assertLessEqual(pos, pane.editor.document().characterCount() - 1)

    def test_reopen_after_close_restores(self):
        area = self._make_area()
        pa = self._write_file("a.txt")
        self.assertTrue(area.open_file_in_active(pa))
        pane = area.active_pane
        pane.goto_line(101)
        self.app.processEvents()
        cursor_pos = pane.editor.textCursor().position()

        pane._close_editor()
        self.app.processEvents()
        self.assertTrue(area.open_file_in_active(pa))
        self.app.processEvents()
        self.assertEqual(pane.editor.textCursor().position(), cursor_pos)


if __name__ == "__main__":
    unittest.main()
