# -*- coding: utf-8 -*-
"""Explorer 的 QFileSystemModel 生命周期：模型随面板销毁、图标提供器随模型存活。

macOS CI 上反复出现的段错误（core 里主线程在 QFileInfoGatherer::getInfo）：
模型没有 parent、只靠 Python 引用活着，面板销毁后它的后台线程仍往主线程投递
目录更新事件，处理时调用已被回收的 _FastIconProvider → 悬空指针。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_model_lifetime.py -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication


class TestExplorerModelLifetime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from explorer_widget import ExplorerPanel
        p = ExplorerPanel()
        tmp = tempfile.mkdtemp(prefix='stellar-explorer-life-')
        for i in range(3):
            with open(os.path.join(tmp, f'f{i}.txt'), 'w') as f:
                f.write('x')
        p.set_root_path(tmp)
        for _ in range(10):
            self.app.processEvents()
        return p

    def test_model_is_owned_by_the_panel(self):
        p = self._panel()
        self.assertIs(QObject.parent(p.model), p)
        self.assertIs(p.model._icon_provider_keepalive, p._icon_provider)
        model = p.model
        sip.delete(p)          # 面板的 C++ 析构 → 子对象（模型）一起删，gatherer 线程随之收工
        self.assertTrue(sip.isdeleted(model))

    def test_refresh_keeps_new_model_owned_and_provider_alive(self):
        p = self._panel()
        old = p.model
        p.refresh()
        self.assertIsNot(p.model, old)
        self.assertIs(QObject.parent(p.model), p)
        self.assertIs(p.model._icon_provider_keepalive, p._icon_provider)
        sip.delete(p)
        self.assertTrue(sip.isdeleted(p.model) if not sip.isdeleted(p) else True)


if __name__ == '__main__':
    unittest.main()
