"""消息框按钮样式不得含 min-width。

回归：main_window_theme.message_box_qss 给 QMessageBox QPushButton 写了 min-width: 76px，
Qt 样式表引擎会据此把按钮 minimumWidth 设成 76 + 左右内边距 = 114，布局只认这个显式
最小值；macOS 上 QMessageBox 按布局最小宽度锁定对话框尺寸，四个按钮被压成 114px，
"Open Releases Page" 等长文案两头截断（v1.30.4 更新提示框实测）。去掉 min-width 后
最小宽度回到按文字计算，每个按钮拿到完整的 sizeHint。

    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_msgbox_button_qss.py -v
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestMessageBoxButtonQss(unittest.TestCase):
    def test_no_min_width_on_message_box_buttons(self):
        from main_window_theme import message_box_qss
        from themes import THEMES
        for name, theme in THEMES.items():
            qss = message_box_qss(theme)
            blocks = re.findall(r'QMessageBox QPushButton[^{]*\{([^}]*)\}', qss)
            self.assertTrue(blocks, f"{name}: 找不到按钮样式块")
            for body in blocks:
                self.assertNotIn('min-width', body,
                                 f"{name}: 按钮样式不能写 min-width（macOS 消息框会把按钮压扁截字）")


if __name__ == '__main__':
    unittest.main()
