"""「Image to CWD」存图目录解析测试

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_image_paste_dir.py -v

背景：曾用 os.getcwd()（主进程 cwd）拼 .images——打包 app 从 Finder 启动
时 cwd 是 /，mkdir 因权限抛异常把整个粘贴静默吞掉（DMG 版无法粘贴图片，
源码运行因从项目目录启动而掩盖）。现统一走 _image_save_dir：
终端真实 cwd → 启动工作目录 → 主目录，创建失败回退临时目录。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication


class TestImageSaveDir(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        global TerminalWidget
        from terminal_widget import TerminalWidget

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='img_dir_test_')
        self.w = TerminalWidget()
        self.w.image_save_local = True

    def tearDown(self):
        self.w.deleteLater()
        self.app.processEvents()
        self._tmp.cleanup()

    def test_uses_terminal_cwd(self):
        # shell 真实 cwd 可得 → 存到 <cwd>/.images
        self.w.get_cwd = lambda: self._tmp.name
        d = self.w._image_save_dir()
        self.assertEqual(d, Path(self._tmp.name) / '.images')
        self.assertTrue(d.is_dir())

    def test_falls_back_to_working_dir(self):
        # shell 未起（get_cwd 返回 None）→ 退启动时的工作目录
        self.w.get_cwd = lambda: None
        self.w._working_dir = self._tmp.name
        d = self.w._image_save_dir()
        self.assertEqual(d, Path(self._tmp.name) / '.images')

    def test_unwritable_cwd_falls_back_to_tempdir(self):
        # cwd 指向不存在的父目录（mkdir 必失败）→ 回退临时目录而不是抛异常
        self.w.get_cwd = lambda: os.path.join(self._tmp.name, 'gone', 'sub')
        d = self.w._image_save_dir()
        self.assertEqual(d, Path(tempfile.gettempdir()) / 'smart_terminal_images')
        self.assertTrue(d.is_dir())

    def test_save_local_off_uses_tempdir(self):
        self.w.image_save_local = False
        d = self.w._image_save_dir()
        self.assertEqual(d, Path(tempfile.gettempdir()) / 'smart_terminal_images')

    def test_never_uses_process_cwd(self):
        # 关键回归守卫：主进程 cwd 与终端无关，任何回退都不该用它。
        # 模拟打包 app 场景：进程 cwd 与所有候选目录都不同
        self.w.get_cwd = lambda: None
        self.w._working_dir = None
        d = self.w._image_save_dir()
        self.assertNotEqual(d, Path(os.getcwd()) / '.images')
        self.assertIn(d, (Path.home() / '.images',
                          Path(tempfile.gettempdir()) / 'smart_terminal_images'))


if __name__ == "__main__":
    unittest.main()
