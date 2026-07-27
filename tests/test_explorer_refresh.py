"""Explorer 刷新回归测试：强制全量重扫必须能看到"watcher 漏掉"的变更。

背景：QFileSystemModel 对抓取过的目录节点做永久缓存，旧实现的
setRootPath("")/setRootPath(current) 技巧只会重新枚举根目录本身，已展开
子目录里丢失的变更永远补不回来。真实触发场景是 FSEvents 丢事件 / 网络卷
上远端写入无本地事件——测试里用 DontWatchForChanges 关闭监听来等价模拟
（monkeypatch 模型类，保证 refresh() 重建出来的新模型同样没有 watcher，
这样"新文件出现"只能归因于强制重扫本身）。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_explorer_refresh.py -v
"""
import os
import sys
import tempfile
import shutil
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtGui import QFileSystemModel

import explorer_widget
from explorer_widget import ExplorerPanel, FilteredFileSystemModel


class _UnwatchedModel(FilteredFileSystemModel):
    """关闭文件系统监听的模型：模拟 watcher 丢事件/失效的环境。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOption(QFileSystemModel.Option.DontWatchForChanges, True)


class ExplorerRefreshTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # realpath：Windows 上 TEMP 是 8.3 短路径（RUNNER~1），Qt 返回的是
        # 长路径，先解析成长路径才能与 Qt 侧的路径对得上
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='explorer_refresh_'))
        self.sub = os.path.join(self.tmp, 'subdir')
        os.makedirs(self.sub)
        open(os.path.join(self.tmp, 'root_a.txt'), 'w').close()
        open(os.path.join(self.sub, 'sub_a.txt'), 'w').close()

        # 让面板初始模型与 refresh() 重建的模型都不带 watcher
        self._orig_model_cls = explorer_widget.FilteredFileSystemModel
        explorer_widget.FilteredFileSystemModel = _UnwatchedModel

        # patch 之后构造，_setup_ui 与 refresh() 用的都是 _UnwatchedModel
        self.panel = ExplorerPanel()
        self.assertIsInstance(self.panel.model, _UnwatchedModel)
        self.panel.set_root_path(self.tmp)
        self._wait_loaded(self.panel.model, self.tmp)

    def tearDown(self):
        explorer_widget.FilteredFileSystemModel = self._orig_model_cls
        self.panel.deleteLater()
        self.app.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_loaded(self, model, path, ms=3000):
        """等待模型异步装载 path 的目录内容。"""
        loaded = []
        model.directoryLoaded.connect(loaded.append)
        if model.canFetchMore(model.index(path)):
            model.fetchMore(model.index(path))
        loop = QEventLoop()

        def check(p):
            if p == path:
                loop.quit()

        model.directoryLoaded.connect(check)
        QTimer.singleShot(ms, loop.quit)
        # 可能在 connect 前就已装载完：rowCount > 0 则直接返回
        if model.rowCount(model.index(path)) > 0:
            return
        loop.exec()

    def _expand_sub(self):
        proxy = self.panel._proxy
        idx = proxy.mapFromSource(self.panel.model.index(self.sub))
        self.assertTrue(idx.isValid())
        self.panel.tree_view.expand(idx)
        self._wait_loaded(self.panel.model, self.sub)

    @staticmethod
    def _norm(p):
        """归一化路径再比较：Qt 的 filePath() 用正斜杠，os 路径在 Windows
        上是反斜杠，直接字符串比对在 Windows CI 会失败。"""
        return os.path.normcase(os.path.normpath(p))

    def _fingerprint_keys(self):
        return {self._norm(p) for p in self.panel._auto_refresh_fingerprints}

    def _visible_names(self, dir_path):
        proxy = self.panel._proxy
        parent = proxy.mapFromSource(self.panel.model.index(dir_path))
        return sorted(
            proxy.fileName(proxy.index(r, 0, parent))
            for r in range(proxy.rowCount(parent))
        )

    def test_refresh_rescans_expanded_subdir(self):
        """watcher 失效时，↻ 刷新必须能看到已展开子目录里的新文件（旧实现败）。"""
        self._expand_sub()
        self.assertEqual(self._visible_names(self.sub), ['sub_a.txt'])

        open(os.path.join(self.sub, 'sub_b_new.txt'), 'w').close()
        open(os.path.join(self.tmp, 'root_b_new.txt'), 'w').close()

        self.panel.refresh()
        self._wait_loaded(self.panel.model, self.tmp)
        self._wait_loaded(self.panel.model, self.sub)
        self.app.processEvents()

        self.assertIn('root_b_new.txt', self._visible_names(self.tmp))
        self.assertIn('sub_b_new.txt', self._visible_names(self.sub))

    def test_refresh_preserves_expansion(self):
        """refresh() 重建模型后，之前展开的子目录仍处于展开状态。"""
        self._expand_sub()
        self.panel.refresh()
        self._wait_loaded(self.panel.model, self.tmp)
        self.app.processEvents()
        proxy = self.panel._proxy
        idx = proxy.mapFromSource(self.panel.model.index(self.sub))
        self.assertTrue(idx.isValid())
        self.assertTrue(self.panel.tree_view.isExpanded(idx))

    def test_auto_refresh_detects_subdir_change(self):
        """自动刷新兜底：已展开子目录里的变化也要能触发刷新。"""
        self._expand_sub()
        self.panel.show()  # tick 里有 isVisible() 守卫
        self.app.processEvents()

        self.panel._auto_refresh_tick()  # 第一轮：建基线，不刷新
        self.assertIn(self._norm(self.sub), self._fingerprint_keys())

        open(os.path.join(self.sub, 'sub_c_new.txt'), 'w').close()
        self.panel._auto_refresh_tick()  # 第二轮：发现子目录变化 → refresh
        self._wait_loaded(self.panel.model, self.sub)
        self.app.processEvents()
        self.assertIn('sub_c_new.txt', self._visible_names(self.sub))

    def test_auto_refresh_empty_dir_baseline(self):
        """空目录的基线不能被当成"未初始化"：从空到非空的变化必须触发刷新。"""
        empty_root = os.path.join(self.tmp, 'empty_root')
        os.makedirs(empty_root)
        self.panel.set_root_path(empty_root)
        self._wait_loaded(self.panel.model, empty_root)
        self.panel.show()
        self.app.processEvents()

        self.panel._auto_refresh_tick()  # 建基线（空集合也是合法基线）
        self.assertIn(self._norm(empty_root), self._fingerprint_keys())

        open(os.path.join(empty_root, 'first.txt'), 'w').close()
        self.panel._auto_refresh_tick()  # 旧实现在这里把变化吞成"建基线"
        self._wait_loaded(self.panel.model, empty_root)
        self.app.processEvents()
        self.assertIn('first.txt', self._visible_names(empty_root))


if __name__ == '__main__':
    unittest.main()
