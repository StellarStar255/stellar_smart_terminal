# -*- coding: utf-8 -*-
"""TerminalWidget 键盘交互的 GUI 级测试（offscreen，合成 QKeyEvent）

覆盖 keyPressEvent 的行为约定：
- 裸修饰键（Cmd/Ctrl/Shift/Alt/CapsLock）不滚动历史、不向终端写入——
  回归：在历史区选中文本后按住 Cmd 准备 Cmd+C，第一下 Cmd 曾把视图滚回底部；
- 普通字符输入仍然自动滚回底部并写入后端；
- Cmd+C（StandardKey.Copy）走复制路径：不滚动、不向终端写入；
- Cmd+V（StandardKey.Paste）走粘贴路径，不直接透传按键。

运行方式：
    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_gui_keyboard -v
"""

import os
# 必须在 import PyQt6 之前设置，保证离屏运行
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from terminal_widget import TerminalWidget


def key_event(key, modifiers=Qt.KeyboardModifier.NoModifier, text=""):
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers, text)


class KeyboardBase(unittest.TestCase):
    """公共基类：QApplication 单例 + 带假后端的 widget"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._widgets = []

    def tearDown(self):
        for w in self._widgets:
            try:
                w.cleanup()
            except Exception:
                pass
            w.deleteLater()
        self._widgets = []

    def make_widget(self, history_lines=100, scroll_offset=20):
        """构造一个「正在运行、已向上滚动」的终端：

        - 喂入足量输出制造历史区；
        - 挂一个假后端（keyPressEvent 只检查 _backend is None）；
        - 截获 _write_to_backend，记录而不真正写入。
        """
        w = TerminalWidget()
        self._widgets.append(w)
        w.stream.feed("line\r\n" * (w.term_rows + history_lines))
        w._backend = types.SimpleNamespace(is_running=True)
        w.writes = []
        w._write_to_backend = lambda data: (w.writes.append(data), True)[1]
        w.scroll_offset = scroll_offset
        return w


class TestBareModifierKeys(KeyboardBase):
    """裸修饰键：不滚动、不写入（Cmd+C 复制前的第一下 Cmd 不能毁掉滚动位置）"""

    BARE_MODIFIERS = [
        ("Cmd(macOS)/Ctrl", Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier),
        ("物理Ctrl(macOS)", Qt.Key.Key_Meta, Qt.KeyboardModifier.MetaModifier),
        ("Shift", Qt.Key.Key_Shift, Qt.KeyboardModifier.ShiftModifier),
        ("Alt", Qt.Key.Key_Alt, Qt.KeyboardModifier.AltModifier),
        ("CapsLock", Qt.Key.Key_CapsLock, Qt.KeyboardModifier.NoModifier),
    ]

    def test_bare_modifier_keeps_scroll_position(self):
        for name, key, mod in self.BARE_MODIFIERS:
            with self.subTest(modifier=name):
                w = self.make_widget(scroll_offset=20)
                w.keyPressEvent(key_event(key, mod))
                self.assertEqual(w.scroll_offset, 20,
                                 f"裸 {name} 不应滚动历史")
                self.assertEqual(w.writes, [],
                                 f"裸 {name} 不应向终端写入")

    def test_bare_modifier_event_accepted(self):
        w = self.make_widget()
        ev = key_event(Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
        w.keyPressEvent(ev)
        self.assertTrue(ev.isAccepted())


class TestRegularInputScrolls(KeyboardBase):
    """普通输入：自动滚回底部并写入后端（确认修复没有误伤原行为）"""

    def test_plain_char_scrolls_to_bottom_and_writes(self):
        w = self.make_widget(scroll_offset=20)
        w.keyPressEvent(key_event(Qt.Key.Key_A, text="a"))
        self.assertEqual(w.scroll_offset, 0, "输入字符应滚回底部")
        self.assertEqual(w.writes, [b"a"])

    def test_enter_scrolls_to_bottom(self):
        w = self.make_widget(scroll_offset=15)
        w.keyPressEvent(key_event(Qt.Key.Key_Return, text="\r"))
        self.assertEqual(w.scroll_offset, 0)
        self.assertEqual(len(w.writes), 1)

    def test_at_bottom_stays_at_bottom(self):
        w = self.make_widget(scroll_offset=0)
        w.keyPressEvent(key_event(Qt.Key.Key_A, text="a"))
        self.assertEqual(w.scroll_offset, 0)


class TestCopyPasteShortcuts(KeyboardBase):
    """Cmd+C / Cmd+V：走 GUI 复制粘贴路径，不滚动、不透传按键本身"""

    def test_copy_shortcut_keeps_scroll_and_does_not_write(self):
        w = self.make_widget(scroll_offset=20)
        copied = []
        w._copy_selection_to_clipboard = lambda: copied.append(True)
        # macOS 上 Cmd→ControlModifier，Cmd+C 即 StandardKey.Copy
        w.keyPressEvent(key_event(
            Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier, "c"))
        self.assertEqual(copied, [True], "Cmd+C 应触发复制")
        self.assertEqual(w.scroll_offset, 20, "复制不应滚动历史")
        self.assertEqual(w.writes, [], "Cmd+C 不应向终端写入")

    def test_paste_shortcut_routes_to_paste(self):
        w = self.make_widget(scroll_offset=10)
        pasted = []
        w._paste_from_clipboard = lambda: pasted.append(True)
        w.keyPressEvent(key_event(
            Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier, "v"))
        self.assertEqual(pasted, [True], "Cmd+V 应触发粘贴")
        self.assertEqual(w.writes, [], "按键本身不应透传给终端")


if __name__ == "__main__":
    unittest.main(verbosity=2)
