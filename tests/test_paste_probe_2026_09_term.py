# -*- coding: utf-8 -*-
"""macOS Cmd+V 剪贴板探测的开销回归（2026-09 审查 D 项）

以前每次 Cmd+V：① 无条件先 mkdir <cwd>/.images（纯文本粘贴也留下空目录）；
② 无条件同步跑 40~60ms 的 osascript。现在：图片先落系统临时文件、确认是
图片后才建目录；剪贴板自上次探测起没变（dataChanged 没响 且 changeCount
相同）且上次结果是 NOTHING 就跳过 osascript。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_paste_probe_2026_09_term.py -q
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


class TestPasteProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from terminal_widget import TerminalWidget
        self._tmp = tempfile.TemporaryDirectory(prefix='paste_probe_')
        self.addCleanup(self._tmp.cleanup)
        w = TerminalWidget()
        w.image_save_local = True
        w.get_cwd = lambda: self._tmp.name
        self.writes = []
        w._backend = SimpleNamespace(is_running=True,
                                     write=lambda d: self.writes.append(d) or True)
        self.delivered = []
        w._deliver_media_path = lambda p, prefix='', suffix='': self.delivered.append(p)
        self.token = 7
        w._clipboard_change_token = lambda: self.token
        self.w = w

    def tearDown(self):
        self.w._backend = None
        self.w.deleteLater()
        self.app.processEvents()

    def _run_nothing(self, *args, **kwargs):
        return SimpleNamespace(stdout='NOTHING\n', returncode=0)

    def test_text_paste_does_not_create_images_dir_and_skips_repeat_probe(self):
        with mock.patch('subprocess.run', side_effect=self._run_nothing) as run:
            self.assertFalse(self.w._paste_clipboard_data_macos_native())
            self.assertEqual(run.call_count, 1)
            self.assertFalse((Path(self._tmp.name) / '.images').exists(),
                             "纯文本粘贴不该创建 .images 目录")
            # 剪贴板没变 → 第二次不再跑 osascript
            self.assertFalse(self.w._paste_clipboard_data_macos_native())
            self.assertEqual(run.call_count, 1, "剪贴板未变仍重复跑了 osascript")
            # Qt dataChanged 响了 → 重新探测
            self.w._on_clipboard_data_changed()
            self.assertFalse(self.w._paste_clipboard_data_macos_native())
            self.assertEqual(run.call_count, 2)
            # NSPasteboard changeCount 变了（截图到剪贴板不切应用、Qt 不发 dataChanged）→ 也重探
            self.token = 8
            self.assertFalse(self.w._paste_clipboard_data_macos_native())
            self.assertEqual(run.call_count, 3)
        self.assertFalse((Path(self._tmp.name) / '.images').exists())
        # 临时文件没残留
        leftovers = [p for p in os.listdir(tempfile.gettempdir()) if p.startswith('stellar_paste_')]
        self.assertEqual(leftovers, [])

    def test_no_token_never_skips(self):
        """拿不到 changeCount（无 pyobjc）时绝不跳过探测。"""
        self.w._clipboard_change_token = lambda: None
        with mock.patch('subprocess.run', side_effect=self._run_nothing) as run:
            self.w._paste_clipboard_data_macos_native()
            self.w._paste_clipboard_data_macos_native()
            self.assertEqual(run.call_count, 2)

    def test_image_saved_into_images_dir_after_confirmation(self):
        def run_image(cmd, **kwargs):
            script = cmd[-1]
            m = re.search(r'writeToFileAtomically\("([^"]+)"', script)
            Path(m.group(1)).write_bytes(b'\x89PNG fake')
            return SimpleNamespace(stdout='IMAGE_OK\n', returncode=0)

        with mock.patch('subprocess.run', side_effect=run_image) as run:
            self.assertTrue(self.w._paste_clipboard_data_macos_native())
            self.assertEqual(run.call_count, 1)
            # 图片粘贴后剪贴板没变也要重探（上次不是 NOTHING）
            self.assertTrue(self.w._paste_clipboard_data_macos_native())
            self.assertEqual(run.call_count, 2)
        images = Path(self._tmp.name) / '.images'
        self.assertTrue(images.is_dir())
        self.assertEqual(len(self.delivered), 2)
        for p in self.delivered:
            self.assertEqual(Path(p).parent, images)
            self.assertTrue(Path(p).is_file())
            self.assertEqual(Path(p).read_bytes(), b'\x89PNG fake')
        leftovers = [p for p in os.listdir(tempfile.gettempdir()) if p.startswith('stellar_paste_')]
        self.assertEqual(leftovers, [])

    def test_osascript_failure_reprobes_next_time(self):
        def boom(*a, **k):
            raise OSError("no osascript")
        with mock.patch('subprocess.run', side_effect=boom) as run:
            self.assertFalse(self.w._paste_clipboard_data_macos_native())
            self.assertFalse(self.w._paste_clipboard_data_macos_native())
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
