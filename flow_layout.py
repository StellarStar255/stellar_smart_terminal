"""
FlowLayout - 自动换行的流式布局
根据容器宽度自动将子控件排列为多行
"""
from PyQt6.QtWidgets import QLayout, QSizePolicy
from PyQt6.QtCore import Qt, QRect, QSize, QPoint


class FlowLayout(QLayout):
    """自动换行布局：子控件按从左到右排列，超出宽度时自动换行"""

    def __init__(self, parent=None, h_spacing=5, v_spacing=5):
        super().__init__(parent)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _effective_item_size(self, item):
        """获取布局项的有效尺寸，匹配 QToolBar 的行为：
        使用 sizeHint 与 minimumSizeHint 的较大值，
        确保自定义样式控件（如带 stylesheet 的 QCheckBox）获得正确尺寸"""
        size = item.sizeHint()
        widget = item.widget()
        if widget:
            size = size.expandedTo(widget.minimumSizeHint())
        return size

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        row_height = 0

        for item in self._items:
            widget = item.widget()
            if widget and not widget.isVisible():
                continue

            item_size = self._effective_item_size(item)
            next_x = x + item_size.width() + self._h_spacing

            if x > effective.x() and next_x - self._h_spacing > effective.right():
                x = effective.x()
                y += row_height + self._v_spacing
                next_x = x + item_size.width() + self._h_spacing
                row_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item_size))

            x = next_x
            row_height = max(row_height, item_size.height())

        return y + row_height - rect.y() + m.bottom()
