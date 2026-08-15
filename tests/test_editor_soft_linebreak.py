"""U+2028 软换行进入编辑器缓冲区的回归测试

背景：QPlainTextEdit 原生 Shift+Return 插入 U+2028（行分隔符），两个视觉行
挤在同一个 QTextDocument block 里。后果三连：语法高亮按块着色（第二行被染成
第一行的颜色，如 `## 注释` 后的 `cd` 命令整行变注释色）、行号区与视觉行错位
（软换行行没有行号）、当前行高亮条双倍高。且保存时 U+2028 原样写入文件，
bash/python 不认它是换行——不止显示错，文件语义也是坏的。

修复：Shift+Enter 视同普通回车插入真实换行；粘贴内容中的
U+2028/U+2029/\r 一律归一化为 \n。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LS = '\u2028'   # LINE SEPARATOR（Shift+Return 的原生产物）
PS = '\u2029'   # PARAGRAPH SEPARATOR


class TestSoftLinebreakNormalization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _editor(self):
        from file_editor import CodeEditor
        ed = CodeEditor()
        ed.resize(600, 400)
        return ed

    def test_shift_enter_creates_real_block(self):
        """Shift+Enter 插入真实换行（新 block），而不是 U+2028 软换行"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        ed = self._editor()
        ed.setPlainText("## comment line")
        cursor = ed.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        ed.setTextCursor(cursor)

        QTest.keyClick(ed, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClicks(ed, "cd /tmp")

        self.assertEqual(ed.document().blockCount(), 2)
        self.assertNotIn(LS, ed.toPlainText())
        # 两行各自成块：第二行文本独立（高亮/行号才会按行工作）
        self.assertEqual(ed.document().findBlockByNumber(1).text(), "cd /tmp")
        ed.deleteLater()

    def test_paste_normalizes_separators(self):
        """粘贴含 U+2028/U+2029/\\r 的文本：全部归一化为真实换行"""
        from PyQt6.QtCore import QMimeData
        ed = self._editor()
        md = QMimeData()
        md.setText(f"## header{LS}cd /tmp{PS}echo a\r\necho b\recho c")
        ed.insertFromMimeData(md)

        text = ed.toPlainText()
        for ch in (LS, PS, '\r'):
            self.assertNotIn(ch, text)
        self.assertEqual(ed.document().blockCount(), 5)
        self.assertEqual(
            text.split('\n'),
            ["## header", "cd /tmp", "echo a", "echo b", "echo c"])
        ed.deleteLater()

    def test_plain_paste_unaffected(self):
        """普通多行粘贴行为不变"""
        from PyQt6.QtCore import QMimeData
        ed = self._editor()
        md = QMimeData()
        md.setText("line1\nline2")
        ed.insertFromMimeData(md)
        self.assertEqual(ed.document().blockCount(), 2)
        self.assertEqual(ed.toPlainText(), "line1\nline2")
        ed.deleteLater()


if __name__ == '__main__':
    unittest.main()
