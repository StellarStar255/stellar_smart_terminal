"""大量长名称预设下，底部条目仍完整可见且可点击。"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from dialogs import PresetDialog
import themes

_app = None


def _application():
    global _app
    _app = QApplication.instance() or QApplication([])
    return _app


@pytest.mark.parametrize('title', [None, '管理本地快速命令'])
@pytest.mark.parametrize('theme', ['午夜黑', '浅色'])
@pytest.mark.parametrize('size', [(600, 450), (820, 600)])
def test_last_long_preset_can_be_scrolled_to_and_clicked(title, theme, size):
    app = _application()
    presets = [{'name': f'{i}: ' + '很长的命令名称 ssh server --argument ' * 12,
                'commands': [f'echo {i}']} for i in range(100)]
    dialog = PresetDialog(presets, title=title, theme=themes.THEMES[theme])
    try:
        dialog.resize(*size)
        dialog.show()
        app.processEvents()
        view = dialog.preset_list
        assert not view.horizontalScrollBar().isVisible()
        assert view.verticalScrollBar().isVisible()
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
        app.processEvents()
        last = view.item(99)
        rect = view.visualItemRect(last)
        assert view.viewport().rect().contains(rect)
        assert rect.height() == view.visualItemRect(view.item(0)).height()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        assert view.currentRow() == 99
        assert dialog.name_edit.text() == presets[99]['name']
        assert dialog.commands_edit.toPlainText() == 'echo 99'
        assert last.toolTip() == presets[99]['name']
        assert dialog.get_presets() == presets
        dialog.name_edit.setText('更新后的长名称 ' * 50)
        assert last.toolTip() == dialog.name_edit.text()
        assert not view.horizontalScrollBar().isVisible()
        dialog._add_preset()
        app.processEvents()
        assert view.viewport().rect().contains(view.visualItemRect(view.currentItem()))
    finally:
        dialog.close()
        dialog.deleteLater()


def test_multiline_name_displays_on_one_row_without_changing_data():
    _application()
    name = '第一行\n第二行\r\n第三行'
    dialog = PresetDialog([{'name': name, 'commands': ['echo ok']}])
    try:
        item = dialog.preset_list.item(0)
        assert item.text() == '第一行 第二行 第三行'
        assert item.toolTip() == name
        assert dialog.get_presets()[0]['name'] == name
    finally:
        dialog.deleteLater()
