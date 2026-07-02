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


@unittest.skipUnless(sys.platform == "darwin", "Cmd+Up/Down 是 macOS 专属键位")
class TestCmdJumpShortcuts(KeyboardBase):
    """Cmd+↓ / Cmd+↑：跳到历史最底部/最顶部"""

    def test_cmd_down_jumps_to_bottom(self):
        w = self.make_widget(scroll_offset=20)
        w.keyPressEvent(key_event(
            Qt.Key.Key_Down, Qt.KeyboardModifier.ControlModifier))
        self.assertEqual(w.scroll_offset, 0)
        self.assertEqual(w.writes, [], "Cmd+↓ 不应向终端写入")

    def test_cmd_up_jumps_to_top(self):
        w = self.make_widget(scroll_offset=0)
        w.keyPressEvent(key_event(
            Qt.Key.Key_Up, Qt.KeyboardModifier.ControlModifier))
        self.assertEqual(w.scroll_offset, w._get_history_count())
        self.assertGreater(w.scroll_offset, 0, "应存在历史可滚动")
        self.assertEqual(w.writes, [])

    def test_cmd_shift_down_not_consumed_by_jump_handler(self):
        # Ctrl+Shift+Down 是「降低不透明度」的窗口级快捷键。真实应用中它在到达
        # 终端前就被窗口级 QAction 消费；这里只验证跳底 handler 带 Shift 时不吞键,
        # 事件照常走「发送到终端」路径（有写入即证明落到了透传分支）。
        w = self.make_widget(scroll_offset=20)
        w.keyPressEvent(key_event(
            Qt.Key.Key_Down,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier))
        self.assertNotEqual(w.writes, [], "带 Shift 时应透传给终端而非被跳底 handler 吞掉")


class TestCopyPasteShortcuts(KeyboardBase):
    """Cmd+C / Cmd+V：走 GUI 复制粘贴路径，不滚动、不透传按键本身"""

    @unittest.skipUnless(sys.platform == "darwin",
                         "Windows/Linux 上 Ctrl+C 无选区时语义是 SIGINT 而非复制")
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


class TestBracketedPasteWrite(KeyboardBase):
    """_write_paste 的 Bracketed Paste 包裹与「末尾换行」处理。

    回归：Claude Code 登录「Paste code here」是单行输入且启用了 bracketed paste。
    三击整行复制 OAuth code 时剪贴板常带尾随换行，_prepare_paste_text 会把它变成
    末尾的 \\r。若不剥掉，\\r 会被夹进 ESC[200~…ESC[201~，成为 code 的一部分，
    导致 "Invalid code / 请确认复制了完整 code"。cmd(Windows Terminal) 会剥掉，
    故 cmd 正常而本终端报错。
    """

    CODE = "abcDEF123-_xyz#xJruZmAv0LHZK9_hRNb79-nhLXqiKKXlmoKNmZ7lHSQ"

    def _paste(self, w, text, bracketed):
        w.screen._bracketed_paste = bracketed
        w.writes = []
        w._write_paste(w._prepare_paste_text(text))
        return w.writes[0]

    def test_bracketed_strips_trailing_newline(self):
        w = self.make_widget()
        for suffix in ("", "\n", "\r\n", "\r", "\n\n"):
            with self.subTest(suffix=repr(suffix)):
                out = self._paste(w, self.CODE + suffix, bracketed=True)
                self.assertEqual(
                    out,
                    b"\x1b[200~" + self.CODE.encode() + b"\x1b[201~",
                    "bracketed 粘贴的 code 里不应残留尾随 \\r（否则校验失败）")

    def test_bracketed_preserves_internal_newlines(self):
        # 多行粘进 TUI 编辑器：行内换行要保留（转成 \r），只剥末尾那个。
        w = self.make_widget()
        out = self._paste(w, "line1\nline2\n", bracketed=True)
        self.assertEqual(out, b"\x1b[200~line1\rline2\x1b[201~")

    def test_non_bracketed_keeps_trailing_cr(self):
        # 普通 shell 粘贴（未启用 bracketed）：末尾 \r 保留，多行命令照旧逐行执行。
        w = self.make_widget()
        out = self._paste(w, self.CODE + "\n", bracketed=False)
        self.assertEqual(out, self.CODE.encode() + b"\r")


if __name__ == "__main__":
    unittest.main(verbosity=2)
