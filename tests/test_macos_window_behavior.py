"""macOS 原生窗口行为（Mission Control / Cmd+` 循环）要真的设上。

日志里出现过三次「设置 macOS 窗口属性失败: 'NoneType' object has no attribute
'windows'」：`from AppKit import NSApp` 拿到 None。改用 NSApplication.sharedApplication()
并优先通过 winId 找到自己的 NSWindow，而不是靠标题扫描。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_macos_window_behavior.py -q
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402


class _FakeNSWindow:
    def __init__(self, title):
        self._title = title

    def title(self):
        return self._title


class _FakeView:
    def __init__(self, window):
        self._window = window

    def window(self):
        return self._window


@unittest.skipUnless(sys.platform == 'darwin', 'macOS only')
class SetupMacosWindow(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _win(self):
        import main_window
        from PyQt6.QtGui import QCloseEvent
        win = main_window.MainWindow()
        win.setWindowTitle('NOT_MATCHING_TITLE')
        # 测试跑在 offscreen 上；被测代码只在 cocoa 平台下才碰 winId，这里假装是 cocoa
        # （AppKit / objc 都换成假模块，不会真的去解指针）
        p = mock.patch.object(QApplication, 'platformName', staticmethod(lambda: 'cocoa'))
        p.start()
        self.addCleanup(p.stop)

        def _dispose():
            QApplication.sendEvent(win, QCloseEvent())
            win.deleteLater()
            for _ in range(3):
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
        self.addCleanup(_dispose)
        return win

    def test_window_found_via_winid_not_title(self):
        import main_window
        win = self._win()
        mine = _FakeNSWindow('whatever the native title is')
        other = _FakeNSWindow('Some Other Window')
        fake_app = types.SimpleNamespace(windows=lambda: [other])
        fake_appkit = types.SimpleNamespace(
            NSApplication=types.SimpleNamespace(sharedApplication=lambda: fake_app))
        fake_objc = types.SimpleNamespace(objc_object=lambda c_void_p=None: _FakeView(mine))
        with mock.patch.dict(sys.modules, {'AppKit': fake_appkit, 'objc': fake_objc}), \
                mock.patch.object(main_window, '_macos_native_cache', True), \
                mock.patch.object(win, '_apply_macos_window_behavior') as apply:
            win._setup_macos_window()
        apply.assert_called_once_with(mine)

    def test_title_scan_fallback_when_winid_lookup_fails(self):
        import main_window
        win = self._win()
        mine = _FakeNSWindow('NOT_MATCHING_TITLE')
        other = _FakeNSWindow('Some Other Window')
        fake_app = types.SimpleNamespace(windows=lambda: [other, mine])
        fake_appkit = types.SimpleNamespace(
            NSApplication=types.SimpleNamespace(sharedApplication=lambda: fake_app))

        def _boom(c_void_p=None):
            raise RuntimeError('no such view')
        fake_objc = types.SimpleNamespace(objc_object=_boom)
        with mock.patch.dict(sys.modules, {'AppKit': fake_appkit, 'objc': fake_objc}), \
                mock.patch.object(main_window, '_macos_native_cache', True), \
                mock.patch.object(win, '_apply_macos_window_behavior') as apply:
            win._setup_macos_window()
        apply.assert_called_once_with(mine)


if __name__ == '__main__':
    unittest.main()
