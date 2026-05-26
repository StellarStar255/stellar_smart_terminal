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

        # 第一遍：将 items 按行分组，计算每行高度
        rows = []  # [(row_height, [(item, item_size, x_pos), ...])]
        x = effective.x()
        row_height = 0
        current_row = []

        for item in self._items:
            widget = item.widget()
            if widget and not widget.isVisible():
                continue

            item_size = self._effective_item_size(item)
            # 所有项（含分隔符）都使用相同的 h_spacing。
            # 间距只加在每项之后，若分隔符特殊处理为 0，则它前面有 5px、后面 0px，
            # 会导致分隔线左右不对称（偏右）；统一用 h_spacing 才能左右对称。
            spacing = self._h_spacing
            next_x = x + item_size.width() + spacing

            if x > effective.x() and next_x - spacing > effective.right():
                rows.append((row_height, current_row))
                current_row = []
                x = effective.x()
                next_x = x + item_size.width() + spacing
                row_height = 0

            current_row.append((item, item_size, x))
            x = next_x
            row_height = max(row_height, item_size.height())

        if current_row:
            rows.append((row_height, current_row))

        # 计算所有行的总高度
        content_height = 0
        if rows:
            for row_h, _ in rows:
                content_height += row_h
            content_height += self._v_spacing * (len(rows) - 1)

        # 第二遍：整体垂直居中后，逐行放置 items（行内也垂直居中）
        available_h = effective.height()
        if available_h > content_height and content_height > 0 and not test_only:
            y = effective.y() + (available_h - content_height) // 2
        else:
            y = effective.y()

        for row_h, row_items in rows:
            if not test_only:
                for item, item_size, x_pos in row_items:
                    y_offset = (row_h - item_size.height()) // 2
                    item.setGeometry(QRect(QPoint(x_pos, y + y_offset), item_size))
            y += row_h + self._v_spacing

        total_height = y - self._v_spacing - rect.y() + m.bottom() if rows else 0
        return total_height
