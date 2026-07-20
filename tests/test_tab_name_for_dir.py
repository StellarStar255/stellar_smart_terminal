"""标签名推导守卫：Finder 工具栏/Dock 启动器传来的尾斜杠路径

回归 v1.14.24：os.path.basename('.../foo/') == '' 导致标签名退回整条路径。
_quick_launch_with_dir 先 normpath 再取名，这里覆盖 normpath 后的输入。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTabNameForDir(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        from main_window import MainWindow
        cls.name = staticmethod(MainWindow._tab_name_for_dir)

    def test_normpath_output_gives_leaf(self):
        # _quick_launch_with_dir 会先 normpath，模拟其输出
        p = os.path.normpath('/Users/admin/Documents/Zhiyuan_Mac/basic_templates_mac/')
        self.assertEqual(self.name(p), 'basic_templates_mac')

    def test_plain_path_leaf(self):
        self.assertEqual(self.name('/a/b/my_project'), 'my_project')

    def test_root_falls_back_to_path(self):
        # 根目录无末级名，退回整条路径而不是空串
        self.assertEqual(self.name('/'), '/')

    def test_normpath_collapses_dot_segments(self):
        p = os.path.normpath('/a/b/./c/../c')
        self.assertEqual(self.name(p), 'c')


if __name__ == '__main__':
    unittest.main()
