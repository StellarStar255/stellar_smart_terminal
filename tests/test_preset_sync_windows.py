"""预设是全局配置：一个窗口改了预设，其它已开窗口的下拉框要立刻跟上。

以前每个窗口启动时各读一份预设进内存，管理框只刷新自己那个窗口，
其它窗口要重启才能看到新预设（用户在双窗口下实测）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_preset_sync_windows.py -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


def _preset(name):
    return {'name': name, 'command': f'echo {name}', 'working_dir': ''}


class PresetSyncAcrossWindowsTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import main_window
        self.windows = []
        for _ in range(2):
            w = main_window.MainWindow()
            w.presets = [_preset('zsh'), _preset('Claude')]
            w._presets_modified = False
            w.last_preset_index = 1
            w._populate_presets()
            w.show()
            self.windows.append(w)
        self.app.processEvents()

    def tearDown(self):
        for w in self.windows:
            w.close()
            w.deleteLater()
        for _ in range(5):
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    @staticmethod
    def _names(w):
        return [w.preset_combo.itemText(i) for i in range(w.preset_combo.count())]

    def test_new_preset_shows_up_in_other_window(self):
        a, b = self.windows
        self.assertEqual(b.preset_combo.currentText(), 'Claude')

        a._apply_edited_presets([_preset('zsh'), _preset('Claude'),
                                 _preset('Claude (with proxy 10808)')])

        self.assertIn('Claude (with proxy 10808)', self._names(b))
        self.assertEqual(b.presets, a.presets)
        self.assertIsNot(b.presets, a.presets)          # 各窗口独立副本
        # 接收方只是采用磁盘内容，不能被标成「本窗口改过」
        self.assertFalse(b._presets_modified)
        self.assertTrue(a._presets_modified)
        # B 窗口原先选着 Claude，同步后仍选 Claude
        self.assertEqual(b.preset_combo.currentText(), 'Claude')

    def test_selection_falls_back_when_current_preset_deleted(self):
        a, b = self.windows
        a._apply_edited_presets([_preset('zsh')])
        self.assertEqual(self._names(b), ['zsh'])
        self.assertEqual(b.preset_combo.currentIndex(), 0)


if __name__ == '__main__':
    unittest.main()
