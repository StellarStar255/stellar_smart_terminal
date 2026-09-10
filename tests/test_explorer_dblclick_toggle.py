"""Explorer 双击目录必须能展开也能收起。

背景：QTreeView 自带 expandsOnDoubleClick，在发完 doubleClicked 信号后
还会再切换一次展开状态，与 _on_double_click 里的切换互相抵消。首次双击
时子项尚未装载、Qt 那次不生效所以能展开；之后每次双击都是「收起再展开」，
表现为文件夹弹开后再也合不上（Ubuntu 上用户实测）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_dblclick_toggle.py -v
"""
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEventLoop, QTimer, Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from explorer_widget import ExplorerPanel  # noqa: E402


class ExplorerDoubleClickToggleTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='explorer_dbl_'))
        self.sub = os.path.join(self.tmp, 'subdir')
        os.makedirs(self.sub)
        open(os.path.join(self.sub, 'child.txt'), 'w').close()
        self.panel = ExplorerPanel()
        self.panel.resize(400, 500)
        self.panel.show()
        self.panel.set_root_path(self.tmp)
        self._wait_loaded(self.tmp)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_loaded(self, path, ms=3000):
        model = self.panel.model
        idx = model.index(path)
        if model.canFetchMore(idx):
            model.fetchMore(idx)
        if model.rowCount(idx) > 0:
            self.app.processEvents()
            return
        loop = QEventLoop()
        model.directoryLoaded.connect(lambda p: loop.quit() if p == path else None)
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
        self.app.processEvents()

    def _sub_index(self):
        idx = self.panel._proxy.mapFromSource(self.panel.model.index(self.sub))
        self.assertTrue(idx.isValid())
        return idx

    def _dblclick(self):
        """对 subdir 那一行发一次双击；索引每次重取（proxy 索引会失效）。"""
        tv = self.panel.tree_view
        idx = self._sub_index()
        tv.scrollTo(idx)
        QTest.mouseDClick(tv.viewport(), Qt.MouseButton.LeftButton,
                          pos=tv.visualRect(idx).center())
        self.app.processEvents()
        return tv.isExpanded(self._sub_index())

    def test_double_click_toggles_loaded_folder(self):
        # 预热：第一下双击 Qt 可能因 pressedIndex 未就绪而当成单击，
        # 最多两下把目录弄到展开态并装载子项
        for _ in range(2):
            if self._dblclick():
                break
        self.assertTrue(self.panel.tree_view.isExpanded(self._sub_index()))
        self._wait_loaded(self.sub)
        # 子项已装载后双击：必须收起（修复前 Qt 自带的双击展开会再把它掀开）
        self.assertFalse(self._dblclick(), "double-click should collapse")
        # 再双击：又能展开
        self.assertTrue(self._dblclick(), "double-click should expand again")
        self.assertFalse(self._dblclick(), "double-click should collapse again")

if __name__ == '__main__':
    unittest.main()
