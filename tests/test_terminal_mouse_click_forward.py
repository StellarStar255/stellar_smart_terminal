"""终端鼠标模式下单击转发测试。

回归：程序启用鼠标上报模式（mouse mode）时，无拖动的左键单击应作为
press+release 的 SGR 鼠标事件转发给程序，让 TUI 里的可点击界面（如
Claude Code 的选项菜单、lazygit、fzf）能响应点击——而不是被吞掉或错发方向键。
未开鼠标模式时则保持本地行编辑光标定位，不发 SGR 鼠标序列。

    QT_QPA_PLATFORM=offscreen python3 -m unittest tests.test_terminal_mouse_click_forward -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMouseClickForward(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.stream.feed('x\r\n' * 60)
        # 让 _send_mouse_event 不被 backend 为 None 的守卫挡掉，并捕获写出的字节
        w._backend = object()
        w._writes = []
        w._write_to_backend = lambda data, _ws=w._writes: _ws.append(data)
        # 点击转发是 opt-in 设置（默认关闭），测试聚焦鼠标模式本身的行为
        w.set_mouse_click_forward_enabled(True)
        return w

    def _click(self, w, abs_cell=(12, 5), rel_cell=(2, 5)):
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QPointF, QEvent, Qt
        w._pos_to_absolute_cell = lambda pos, _c=abs_cell: _c
        w._pos_to_cell = lambda pos, _c=rel_cell: _c
        w.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(9, 9),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        w.mouseReleaseEvent(QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(9, 9),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier))

    def test_click_forwarded_in_mouse_mode(self):
        w = self._widget()
        w._mouse_mode = True
        self._click(w, abs_cell=(12, 5), rel_cell=(2, 5))
        blob = b''.join(w._writes)
        # rel_cell=(row=2, col=5) → SGR 坐标 1-based：col+1=6, row+1=3
        self.assertIn(b'\x1b[<0;6;3M', blob, "应转发左键按下 (SGR press)")
        self.assertIn(b'\x1b[<0;6;3m', blob, "应转发左键释放 (SGR release)")
        # 鼠标模式下不应错发方向键
        self.assertNotIn(b'\x1b[C', blob)
        self.assertNotIn(b'\x1b[D', blob)

    def test_mouse_mode_with_forwarding_off_sends_nothing(self):
        """Claude Code 开了鼠标上报、用户没开"点击转发"：单击必须什么都不发。
        以前会退到"定位光标"路径往程序里灌方向键，Claude Code 的选项框收到
        ESC 开头的序列就当成取消——一点选项就全没了。"""
        w = self._widget()
        w.set_mouse_click_forward_enabled(False)
        w._mouse_mode = True
        # 点在光标所在行、且列不同 → 旧逻辑必然发方向键
        w.screen.cursor.y = 2
        w.screen.cursor.x = 20
        self._click(w, abs_cell=(12, 5), rel_cell=(2, 5))
        self.assertEqual(w._writes, [], "鼠标模式下关掉转发就该一个字节都不发")

    def test_cursor_move_is_written_as_one_burst(self):
        """普通 shell 里点击定位光标：整串方向键一次写出，别让程序读到半截 ESC。"""
        w = self._widget()
        w._mouse_mode = False
        w.screen.cursor.y = 2
        w.screen.cursor.x = 8
        self._click(w, abs_cell=(12, 5), rel_cell=(2, 5))
        self.assertEqual(w._writes, [b'\x1b[D' * 3])

    def test_click_not_forwarded_without_mouse_mode(self):
        w = self._widget()
        w._mouse_mode = False
        self._click(w, abs_cell=(12, 5), rel_cell=(2, 5))
        blob = b''.join(w._writes)
        self.assertNotIn(b'\x1b[<', blob, "未开鼠标模式不应发 SGR 鼠标序列")


if __name__ == '__main__':
    unittest.main()
