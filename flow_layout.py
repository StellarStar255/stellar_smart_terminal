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
        self._item_sizes = {}  # 缓存：widget id -> 实际渲染尺寸

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
            item = self._items.pop(index)
            widget = item.widget()
            if widget:
                self._item_sizes.pop(id(widget), None)
            return item
        return None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)
        # 第二遍：第一遍后控件可能被 Qt 强制为更大的最小尺寸（如带 stylesheet
        # 的 QCheckBox 的 indicator 大小与 sizeHint 不一致），
        # 用实际尺寸重新布局以消除重叠
        self._capture_actual_sizes()
        self._do_layout(rect, test_only=False)

    def _capture_actual_sizes(self):
        """捕获控件在第一遍布局后的实际渲染尺寸"""
        for item in self._items:
            widget = item.widget()
            if widget and widget.isVisible():
                actual = widget.size()
                hint = item.sizeHint()
                # 只缓存比 sizeHint 更大的实际尺寸（排除未初始化的默认值 640x480）
                if (actual.width() > hint.width() and actual.width() < hint.width() * 4):
                    self._item_sizes[id(widget)] = actual

    def _effective_size(self, item):
        """获取布局项的有效尺寸：优先使用缓存的实际渲染尺寸"""
        widget = item.widget()
        hint = item.sizeHint()
        if widget:
            cached = self._item_sizes.get(id(widget))
            if cached:
                return hint.expandedTo(cached)
        return hint

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
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

            item_size = self._effective_size(item)
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
