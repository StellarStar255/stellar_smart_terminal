"""
通用小控件
从 main_window.py 拆分出来的不依赖 MainWindow 的控件类
"""
import time

from PyQt6.QtWidgets import (
    QAbstractButton, QApplication, QCheckBox, QComboBox, QCompleter, QLineEdit, QProxyStyle,
    QStyle, QStyledItemDelegate, QStyleOptionButton, QStyleOptionComboBox,
    QStylePainter, QTabBar, QWidget
)
from PyQt6.QtCore import Qt, QTimer, QEvent, QPoint, QPointF, QRectF, QStringListModel, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPalette, QPen

from utils import parse_search_tokens, name_matches_tokens


class SelectAllLineEdit(QLineEdit):
    """点击文字附近时自动全选的输入框；若关联了下拉框（popup_owner），
    点击文字右侧的空白区域则弹出下拉选项列表（而非全选/定位光标）。"""
    # 弹窗刚关闭后的“防抖窗口”：用于聚焦返回时不重新全选等场景（单位：秒）
    _POPUP_REOPEN_GUARD = 0.30
    # 判定“弹窗是被本次点击关掉的”所允许的时序抖动：macOS 原生抓取关闭与本控件
    # 收到 press 几乎同时发生，可能差几毫秒、先后不定，留一点容差兜住（单位：秒）
    _CLICK_CLOSE_EPS = 0.05

    def __init__(self, parent=None, popup_owner=None):
        super().__init__(parent)
        self._pending_selectall = False  # 因聚焦安排的延迟全选，可被空白点击取消
        # 关联的 QComboBox：点击空白处时用它来弹出选项列表
        self._popup_owner = popup_owner
        self._popup_hidden_at = 0.0     # 列表最近一次关闭的时间戳
        self._blank_press_at = 0.0      # 最近一次“空白区按下”的时间戳，用于精确判定开/关
        self._popup_is_visible = False  # view 的显示状态；用于覆盖 macOS popup 抓取下的焦点事件乱序
        self._filtered_view = None      # 已安装事件过滤器的 view
        if popup_owner is not None:
            self._install_popup_filter()

    def set_popup_owner(self, combo):
        self._popup_owner = combo
        self._install_popup_filter()

    def _install_popup_filter(self):
        """在下拉框的 view 上安装事件过滤器，用于记录列表的关闭时间。
        关键：装在 view（QListView，从一开始就存在、对象稳定）而不是装在
        惰性创建的弹窗容器上——经验证关闭时 view 一定会收到 Hide 事件，
        这样无论列表是被本控件、🕘 按钮还是键盘打开，都能可靠捕获关闭。"""
        owner = self._popup_owner
        if owner is None:
            return
        view = owner.view()
        if view is None or view is self._filtered_view:
            return
        view.installEventFilter(self)
        self._filtered_view = view

    def eventFilter(self, obj, event):
        if obj is self._filtered_view:
            if event.type() == QEvent.Type.Show:
                self._popup_is_visible = True
            elif event.type() == QEvent.Type.Hide:
                self._popup_is_visible = False
                self._popup_hidden_at = time.monotonic()
                self._pending_selectall = False
                self.deselect()
        return super().eventFilter(obj, event)

    def _popup_visible(self) -> bool:
        owner = self._popup_owner
        if owner is None:
            return False
        view = owner.view()
        return self._popup_is_visible or (view is not None and view.isVisible())

    def _recently_hidden(self) -> bool:
        return (time.monotonic() - self._popup_hidden_at) < self._POPUP_REOPEN_GUARD

    def _apply_pending_selectall(self):
        if self._pending_selectall:
            self._pending_selectall = False
            self.selectAll()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._popup_owner is not None and (self._popup_visible() or self._recently_hidden()):
            self._pending_selectall = False
            self.deselect()
            return
        reason = event.reason()
        # 只有“真正想编辑”的获得焦点才自动全选：鼠标点击、键盘 Tab / 快捷键切入。
        # 而“弹窗关闭后焦点返回、窗口重新激活”等原因绝不全选——否则会在关闭下拉
        # 列表时把刚清空的选区又选回来，造成选中内容闪一下再消失。
        # 注意：鼠标点击时这里先“安排”全选并延迟执行；若随后的 mousePressEvent
        # 判定点的是空白区（意图开下拉而非编辑），会把它取消掉。
        if reason in (
            Qt.FocusReason.MouseFocusReason,
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
        ):
            self._pending_selectall = True
            QTimer.singleShot(0, self._apply_pending_selectall)

    def _click_in_blank_area(self, x: float) -> bool:
        """判断点击横坐标是否落在文字右侧的空白区域。
        空文本时整行都视为“文字区”，避免一进来就误弹列表。"""
        text = self.text()
        if not text:
            return False
        # 文字左起点：内容区左边距 + Qt 内部水平边距(约 2px)
        left = self.contentsRect().left() + 2
        text_right = left + self.fontMetrics().horizontalAdvance(text)
        # 留一点容差，靠近文字尾部仍按“选中”处理
        return x > text_right + 6

    def _is_blank_click(self, event) -> bool:
        return (self._popup_owner is not None
                and event.button() == Qt.MouseButton.LeftButton
                and self._click_in_blank_area(event.position().x()))

    def mousePressEvent(self, event):
        # 点击右侧空白区域：仅吞掉按下事件、取消聚焦全选；真正的开/关全部推迟到
        # 松开后再做。绝不在 press 里读状态来决定开/关——macOS 上弹窗的鼠标抓取
        # 可能在事件送达本控件前就把列表关掉，导致 press 期的判断不可靠。
        if self._is_blank_click(event):
            self._pending_selectall = False  # 意图是开下拉，不该全选/不该闪烁
            self._blank_press_at = time.monotonic()  # 记下本次按下时刻，供 toggle 精确判定
            event.accept()
            return
        # 点在文字区域：交给基类处理（定位光标/起始选择），首次聚焦时的全选由
        # focusInEvent 安排的延迟全选负责，覆盖掉基类的光标定位。
        super().mousePressEvent(event)

    def note_external_press(self):
        """供 🕘 按钮等外部控件在“按下”时调用：补记一次按下时刻，使外部触发与
        输入框空白点击共用同一套精确的开/关判定（关键是要在原生抓取关闭弹窗的
        同一时刻附近记下，故接到按钮的 pressed 而非 clicked）。"""
        self._blank_press_at = time.monotonic()

    def toggle_popup(self):
        """做“恰好一次”的开/关历史下拉列表：
          - 列表此刻仍显示 → 关闭它；
          - 列表此刻不可见，但它是被“本次点击”关掉的（Hide 时间不早于本次按下，
            即 macOS 原生抓取在这次点击里把它关了）→ 什么都不做，避免反弹重开；
          - 否则（列表本就关着、且不是被这次点击关的）→ 打开它。
        关键：用“本次点击的按下时刻”而非“距上次关闭的固定时间窗”来判定，
        否则关闭后很快再点空白想打开时，会被固定防抖窗误挡（表现为“有时弹有时不弹”）。
        输入框空白点击与 🕘 按钮共用此方法。"""
        owner = self._popup_owner
        if owner is None:
            return
        if self._popup_visible():
            owner.hidePopup()
            return
        closed_by_this_click = (
            self._popup_hidden_at >= self._blank_press_at - self._CLICK_CLOSE_EPS
        )
        if not closed_by_this_click:
            self._install_popup_filter()
            owner.showPopup()

    def mouseReleaseEvent(self, event):
        if self._is_blank_click(event):
            event.accept()
            # 推迟到事件循环空闲再切换：此时按下的隐式抓取已释放、弹窗抓取若要关闭
            # 列表也已完成，弹窗可见性此刻是稳定且可信的。
            QTimer.singleShot(0, self.toggle_popup)
            return
        super().mouseReleaseEvent(event)


class MultiKeywordCompleter(QCompleter):
    """多关键词（空格分隔、AND 子串、大小写不敏感）补全。

    与文件 quick-open 一致：输入 "llm train" 命中同时含 "llm" 和 "train" 的候选
    （如 .../zhiyuan_llm_8_gpu_machine_model_training）。用于目录历史输入框，让
    用户不用记前缀、敲几个词就能快速跳到某个历史目录。

    实现：UnfilteredPopupCompletion + 在 splitPath 里按当前输入重建候选列表，
    弹窗即显示过滤后的全部匹配（Qt 自身不再做前缀过滤）。
    """

    def __init__(self, items=None, parent=None):
        super().__init__(parent)
        self._all = list(items or [])
        self._model = QStringListModel(self._all, self)
        self.setModel(self._model)
        self.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)

    def set_items(self, items):
        self._all = list(items or [])

    def splitPath(self, path):
        tokens = parse_search_tokens(path or "")
        if tokens:
            filtered = [s for s in self._all if name_matches_tokens(s, tokens)]
        else:
            filtered = list(self._all)
        self._model.setStringList(filtered)
        return [path or ""]



class _NoPopupFlashStyle(QProxyStyle):
    """把 SH_Menu_FlashTriggeredItem 关掉的代理样式（其余全部透传给平台样式）。

    QComboBox::hidePopup 若查到该提示为真（macOS 样式默认为真），关闭列表前会先
    “去掉选中高亮 → 等 60ms → 重新高亮 → 等 20ms → 才真正隐藏”，这是模仿原生菜单
    的“选中项闪一下”效果。但我们的下拉用的是 QSS 定制的列表弹窗，闪烁在这里只表现
    为：点空白处关闭时蓝色高亮条灭一下、亮一下，然后列表才消失，看起来像坏了。
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Menu_FlashTriggeredItem:
            return 0
        return super().styleHint(hint, option, widget, returnData)


_no_popup_flash_style = None


def suppress_popup_flash(combo):
    """让 combo 关闭下拉列表时不做“选中项闪烁”，点击外部/选中项后列表立即隐藏。

    实现是给控件挂一个共享的 _NoPopupFlashStyle（不带 base 的 QProxyStyle 会自己
    创建一份平台默认样式，与应用当前样式一致）。设置了样式表时 Qt 会自动用
    QStyleSheetStyle 包住它，QSS 外观不受影响（已用 grab() 逐像素比对验证）。
    """
    global _no_popup_flash_style
    style = _no_popup_flash_style
    if style is not None:
        try:
            from PyQt6 import sip
            if sip.isdeleted(style):
                style = None
        except ImportError:
            pass
    if style is None:
        style = _no_popup_flash_style = _NoPopupFlashStyle()
        app = QApplication.instance()
        if app is not None:
            # 生命周期跟随 QApplication，避免被 Python GC 提前回收
            style.setParent(app)
    combo.setStyle(style)


class QuietPopupComboBox(QComboBox):
    """关闭 popup 时不让列表当前项做最后一次高亮重绘（消失瞬间蓝色高亮条闪烁）。

    实现：进入 hidePopup 时冻结列表 view 的重绘（setUpdatesEnabled(False)）并保持
    冻结，由下次 showPopup 在重新显示前恢复（setUpdatesEnabled(True)）。另外挂
    suppress_popup_flash：macOS 样式的“选中项闪烁”会把真正隐藏推迟 80ms，即使冻结
    了重绘，列表也会在点击后停留一会儿才消失。

    关键：绝不在 hidePopup 里用 QTimer.singleShot 提前恢复更新——macOS 上原生弹窗
    隐藏是异步的，列表窗口可能还在屏上停留一帧；而 setUpdatesEnabled(True) 会隐式
    触发一次 update() 重绘（已实测验证），于是会在“列表即将消失”那一刻把蓝色高亮
    重绘出来，随后窗口才真正隐藏 = 蓝条闪一下。隐藏期间保持冻结、靠 showPopup 恢复，
    此时 view 仍处于隐藏态，重新启用更新不会产生可见重绘。

    另：绝不能在 hidePopup 里清空 view 的当前项/选区——鼠标点选某一项时，Qt 的弹窗
    容器正是在 hidePopup 过程中读取 view 的当前项来提交选择；若提前清空，combo 会以
    无效索引提交（currentIndex 变 -1），导致选中的路径丢失、输入框被清空。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        suppress_popup_flash(self)

    def showPopup(self):
        view = self.view()
        if view is not None:
            view.setUpdatesEnabled(True)
        super().showPopup()

    def hidePopup(self):
        view = self.view()
        if view is not None:
            view.setUpdatesEnabled(False)
        super().hidePopup()


class CenteredComboBox(QComboBox):
    """显示文本居中的 QComboBox。
    策略：让 Qt 样式先绘制无文本的 combo（边框、背景、下拉箭头等），
    再在整个可见按钮区域居中绘制当前文本，避免默认左对齐文本参与绘制。"""

    # 下拉三角颜色：由 _apply_theme 按主题下发（以前写死 #cfd6ff，浅色主题下几乎看不见）
    _arrow_color = "#cfd6ff"

    def set_arrow_color(self, color: str):
        if color and color != self._arrow_color:
            self._arrow_color = color
            self.update()

    def arrow_color(self) -> str:
        return self._arrow_color

    def __init__(self, parent=None):
        super().__init__(parent)
        # 强制使用 Qt 可定制的下拉弹窗：否则 macOS 会用原生 NSMenu，
        # 带勾选标记、定位偏移且无法被 stylesheet 美化（弹窗显得错位、难看）。
        self.setItemDelegate(QStyledItemDelegate(self))
        self.view().setTextElideMode(Qt.TextElideMode.ElideNone)
        self.view().setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._minimum_popup_width = 0
        # 点空白处关闭时不做“高亮灭一下亮一下再消失”的闪烁（macOS 样式默认行为）
        suppress_popup_flash(self)

    def setMinimumPopupWidth(self, width: int):
        self._minimum_popup_width = max(0, width)

    def _popup_width(self) -> int:
        view = self.view()
        content_width = view.sizeHintForColumn(0) if view is not None else 0
        # The list view needs room for the combo frame, delegate padding and the
        # current-item marker area. Without this, short popups elide "English".
        return max(self._minimum_popup_width, content_width + 12)

    def showPopup(self):
        popup_width = self._popup_width()
        view = self.view()
        if view is not None:
            view.setMinimumWidth(popup_width)
            view.setTextElideMode(Qt.TextElideMode.ElideNone)

        super().showPopup()
        # 修正弹窗水平位置：当 combo 不在其父容器最左侧时（例如前面有 "Language:" 标签），
        # Qt 会把弹窗对齐到容器左边而非 combo 左边，导致弹窗向左错位。
        # 这里把弹窗左边强制对齐到 combo 左边，并做屏幕边界钳制。
        view = self.view()
        container = view.parentWidget() if view is not None else None
        if container is None:
            return
        container.resize(max(container.width(), popup_width), container.height())
        combo_left = self.mapToGlobal(QPoint(0, 0)).x()
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            combo_left = min(combo_left, avail.right() - container.width() + 1)
            combo_left = max(combo_left, avail.left())
        geo = container.geometry()
        if geo.x() != combo_left:
            container.move(combo_left, geo.y())

    def addItem(self, *args):
        super().addItem(*args)
        self._center_item(self.count() - 1)

    def insertItem(self, index, *args):
        super().insertItem(index, *args)
        self._center_item(index)

    def _center_item(self, index: int):
        if index >= 0:
            self.setItemData(
                index,
                Qt.AlignmentFlag.AlignCenter,
                Qt.ItemDataRole.TextAlignmentRole,
            )

    def paintEvent(self, event):
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        text = opt.currentText
        opt.currentText = ""

        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)

        # 用样式按 stylesheet(margin/border/padding/箭头宽度)算出的子区域来定位，
        # 避免手算 rect.right() 时忽略 margin-right 导致三角顶到/越出控件右边框。
        style = self.style()
        text_rect = style.subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt,
            QStyle.SubControl.SC_ComboBoxEditField, self)
        arrow_rect = style.subControlRect(
            QStyle.ComplexControl.CC_ComboBox, opt,
            QStyle.SubControl.SC_ComboBoxArrow, self)
        painter.setPen(self.palette().color(QPalette.ColorRole.Text))
        painter.setFont(self.font())
        # 文本超出可用宽度时用「…」省略，否则居中绘制会把首尾都截掉（如 "Midnight Black"
        # 变成 "idnight Blac"）。先按文字区宽度做右省略，再居中。
        elided = painter.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, text_rect.width()
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, elided)

        # 手绘一个简洁的下拉三角，居中于样式给出的箭头区域
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx = arrow_rect.center().x()
        cy = arrow_rect.center().y() + 1
        half_w = 4.0
        height = 4.0
        path = QPainterPath()
        path.moveTo(cx - half_w, cy - height / 2)
        path.lineTo(cx + half_w, cy - height / 2)
        path.lineTo(cx, cy + height / 2)
        path.closeSubpath()
        painter.fillPath(path, QColor(self._arrow_color))


class InlineRenameEdit(QLineEdit):
    """就地重命名用的小输入框：回车/失焦提交，Esc 取消。"""

    committed = pyqtSignal(str)   # 提交，参数为输入文本
    cancelled = pyqtSignal()      # 取消（Esc）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._done = False
        self.returnPressed.connect(self._commit)

    def _commit(self):
        if self._done:
            return
        self._done = True
        self.committed.emit(self.text())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            if not self._done:
                self._done = True
                self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        # 补全下拉弹出时焦点会短暂离开输入框：那不是"用户点了别处"，别提交
        if event.reason() == Qt.FocusReason.PopupFocusReason:
            return
        self._commit()


class DetachableTabBar(QTabBar):
    """可拖拽分离的标签栏"""

    tab_detach_requested = pyqtSignal(int, QPoint)  # 发送要分离的tab索引和全局坐标
    tab_rename_requested = pyqtSignal(int)          # 双击某个 tab 请求就地重命名

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_start_pos = None
        self._drag_tab_index = -1
        self._is_dragging = False
        # 竖向拖出标签栏这么多像素就进入影子拖拽（横向拖动交给 QTabBar 重排）
        self._detach_threshold = 28
        self._original_cursor = None

    def mouseDoubleClickEvent(self, event):
        # 双击标签 → 就地重命名（双击空白处不处理）
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.tabAt(event.pos())
            if idx >= 0:
                self._reset_drag_state()
                self.tab_rename_requested.emit(idx)
                return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.pos()
            self._drag_tab_index = self.tabAt(event.pos())
            self._original_cursor = self.cursor()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start_pos is None or self._drag_tab_index < 0:
            super().mouseMoveEvent(event)
            return

        # 计算拖拽距离
        diff = event.pos() - self._drag_start_pos

        # 当接近阈值时，改变鼠标光标提供视觉反馈
        if abs(diff.y()) > self._detach_threshold * 0.6:
            self.setCursor(Qt.CursorShape.DragMoveCursor)
        else:
            if self._original_cursor:
                self.setCursor(self._original_cursor)

        # 如果垂直方向拖拽超过阈值，触发分离（影子拖拽，见 _begin_tab_drag）
        if abs(diff.y()) > self._detach_threshold:
            self._is_dragging = True
            global_pos = self.mapToGlobal(event.pos())
            index = self._drag_tab_index
            press_pos = self._drag_start_pos
            self._reset_drag_state()
            # 先把 QTabBar 自己的拖动状态收掉（按原位置补一个松开）：标签在
            # 影子拖拽期间留在原处，别让它跟着横向乱滑、松手时又触发一次重排
            try:
                fake = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(press_pos),
                                   Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                                   Qt.KeyboardModifier.NoModifier)
                super().mouseReleaseEvent(fake)
            except Exception:
                pass  # 收不掉也只是标签滑一下，不影响拖拽结果
            self.tab_detach_requested.emit(index, global_pos)
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._reset_drag_state()
        super().mouseReleaseEvent(event)

    def _reset_drag_state(self):
        self._drag_start_pos = None
        self._drag_tab_index = -1
        self._is_dragging = False
        if self._original_cursor:
            self.setCursor(self._original_cursor)
            self._original_cursor = None


class TabCloseButton(QAbstractButton):
    """标签页右侧的关闭按钮：矢量画的细 ×，平时灰色，悬停才浮出红色圆底。

    以前是 20×20 的红色实心圆 + 字符「×」（靠 emoji 字体渲染，位置飘），而且
    贴着标签右边缘、看上去被挤出标签外。现在控件右侧自带 RIGHT_GUTTER 的空白，
    × 离标签边缘有呼吸感；本身就是 setTabButton 的那个控件（关闭逻辑靠
    sender() is tabButton 识别，不能再包一层容器）。
    """

    DIAMETER = 16
    RIGHT_GUTTER = 6
    # QTabBar 把按钮在整个标签框里垂直居中，而标题文字受 margin-top/padding
    # 影响落得偏低：× 在控件内下移这么多，才与标题文字对齐
    Y_NUDGE = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(self.DIAMETER + 2 + self.RIGHT_GUTTER,
                          self.DIAMETER + 2 + 2 * self.Y_NUDGE)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._x = QColor('#888888')
        self._hover_bg = QColor('#e74c3c')
        self._hover_x = QColor('#ffffff')

    def set_theme(self, theme: dict):
        """按主题取色：× 用次要文字色，悬停圆底用危险色、× 变白。"""
        self._x = QColor(theme.get('text_dim', '#888888'))
        self._hover_bg = QColor(theme.get('danger', '#e74c3c'))
        self._hover_x = QColor('#ffffff')
        self.update()

    def sizeHint(self):
        return self.size()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        d = self.DIAMETER
        circle = QRectF(1, (self.height() - d) / 2 + self.Y_NUDGE, d, d)
        hovered = self.underMouse() or self.isDown()
        if hovered:
            bg = QColor(self._hover_bg)
            if self.isDown():
                bg = bg.darker(115)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawEllipse(circle)
        pen = QPen(self._hover_x if hovered else self._x, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        c = circle.center()
        r = 3.6
        p.drawLine(QPointF(c.x() - r, c.y() - r), QPointF(c.x() + r, c.y() + r))
        p.drawLine(QPointF(c.x() - r, c.y() + r), QPointF(c.x() + r, c.y() - r))
        p.end()


class DropHintOverlay(QWidget):
    """拖标签 / 窗格时，落点上的高亮提示（自绘，替代原来的半透明蓝块 + 方框）。

    三种样子：
    - 'strip'：标签栏落点。追加到末尾时，在最后一个标签右边画一个「标签占位」
      （和真标签同形状、淡色填充、写着被拖标签的标题——松手它就出现在这儿）；
      插到两个标签之间时，只在标签缝里画一根细插入线。标签栏本身不染色。
    - 'area'：分屏 / 窗格落区——内缩的圆角区域，淡色填充 + 细边。
    - 'page'：落在别的窗口内容区——同 'area'，正中再加一个说明胶囊（label）。
    对鼠标透明，只负责画。
    """

    def __init__(self, parent, accent: str = '#667eea', text: str = '#ffffff'):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._accent = QColor(accent)
        self._text = QColor(text)
        self._mode = 'area'
        self._caret_x = None
        self._slot = None         # QRectF：末尾的「标签占位」
        self._slot_title = ''
        self._slot_font = None
        self._text_dim = QColor(text)

    def set_colors(self, accent: str, text: str = '#ffffff'):
        self._accent = QColor(accent)
        self._text = QColor(text)
        self.update()

    def set_mode(self, mode: str, label: str = ''):
        self._mode = mode
        self._label = label
        if mode != 'strip':
            self._caret_x = None
            self._slot = None
        self.update()

    def set_caret_x(self, x):
        """两标签之间的插入线横坐标（本控件坐标）；设了就不画末尾占位。"""
        if x != self._caret_x or self._slot is not None:
            self._caret_x = x
            self._slot = None
            self.update()

    def set_slot(self, rect, title: str = '', font=None, text_color=None):
        """末尾「标签占位」（本控件坐标的 QRectF）；设了就不画插入线。"""
        self._slot = rect
        self._slot_title = title
        self._slot_font = font
        if text_color is not None:
            self._text_dim = QColor(text_color)
        self._caret_x = None
        self.update()

    def _tint(self, alpha):
        c = QColor(self._accent)
        c.setAlpha(alpha)
        return c

    def _paint_slot(self, p):
        """末尾的标签占位：上圆角下直角，与真标签同形；淡色底 + 细边 + 淡标题。"""
        r = QRectF(self._slot)
        rad = 7.0
        path = QPainterPath()
        path.moveTo(r.left(), r.bottom())
        path.lineTo(r.left(), r.top() + rad)
        path.quadTo(r.left(), r.top(), r.left() + rad, r.top())
        path.lineTo(r.right() - rad, r.top())
        path.quadTo(r.right(), r.top(), r.right(), r.top() + rad)
        path.lineTo(r.right(), r.bottom())
        p.setBrush(self._tint(45))
        pen = QPen(self._tint(170), 1.2)
        p.setPen(pen)
        p.drawPath(path)
        # 顶边一条强调色，呼应选中标签的样子
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._accent)
        p.drawRoundedRect(QRectF(r.left() + rad - 2, r.top(), r.width() - 2 * rad + 4, 2), 1, 1)
        if self._slot_title:
            from PyQt6.QtGui import QFontMetrics
            font = self._slot_font or self.font()
            fm = QFontMetrics(font)
            text_r = r.adjusted(12, 2, -12, 0)
            c = QColor(self._text_dim)
            c.setAlpha(210)
            p.setPen(c)
            p.setFont(font)
            p.drawText(text_r, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       fm.elidedText(self._slot_title, Qt.TextElideMode.ElideRight,
                                     int(text_r.width())))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        if self._mode == 'strip':
            if self._slot is not None:
                self._paint_slot(p)
            elif self._caret_x is not None:
                x = max(2.0, min(r.width() - 2.0, float(self._caret_x)))
                top, bottom = r.top() + 7, r.bottom() - 5
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(self._accent)
                p.drawRoundedRect(QRectF(x - 1, top, 2, bottom - top), 1, 1)
            p.end()
            return
        area = r.adjusted(6, 6, -6, -6)
        p.setBrush(self._tint(38))
        p.setPen(QPen(self._tint(200), 1.5))
        p.drawRoundedRect(area, 10, 10)
        if self._mode == 'page' and self._label:
            from PyQt6.QtGui import QFont, QFontMetrics
            font = QFont(self.font())
            font.setPixelSize(13)
            font.setWeight(QFont.Weight.DemiBold)
            fm = QFontMetrics(font)
            w = fm.horizontalAdvance(self._label) + 32
            h = fm.height() + 16
            pill = QRectF(area.center().x() - w / 2, area.center().y() - h / 2, w, h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._accent)
            p.drawRoundedRect(pill, h / 2, h / 2)
            p.setPen(self._text)
            p.setFont(font)
            p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), self._label)
        p.end()


class TabDragPreview(QWidget):
    """拖标签 / 窗格 / 导航条目时跟着光标的「迷你窗口」。

    做成一个小窗口的样子，一眼就知道拖的是窗口：标题栏（红黄绿三个圆点 +
    窗口色圆点 + 省略号截断的标题）+ 窗口内容缩略图（没有缩略图时画几行
    占位的文字线）。按主题配色，圆角、细描边、柔和阴影。
    独立置顶无边框小窗、对鼠标透明、不抢焦点；落在光标右下方，
    QApplication.topLevelAt(光标) 永远查不到它自己。
    """

    SHADOW = 12          # 四周给阴影留的透明边
    RADIUS = 8
    CURSOR_OFFSET = QPoint(14, 10)   # 迷你窗口左上角相对光标的位置
    WIDTH = 300          # 迷你窗口宽度
    TITLE_H = 24         # 标题栏高度
    MAX_BODY_H = 170
    DEFAULT_BODY_H = 120
    MAX_TEXT_W = 300     # 标题文字截断宽度上限（实际还受 WIDTH 限制）

    def __init__(self, title: str, theme: dict = None, dot_color: str = None,
                 thumbnail=None):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        th = theme or {}
        self._body_bg = QColor(th.get('bg_dark', '#1a1a2e'))
        self._title_bg = QColor(th.get('bg_light', '#2d2d44'))
        self._fg = QColor(th.get('text', '#eaeaea'))
        self._dim = QColor(th.get('text_dim', '#888888'))
        self._accent = QColor(th.get('accent', '#667eea'))
        self._dot = QColor(dot_color) if dot_color else QColor(self._accent)
        self._thumb = thumbnail if thumbnail is not None and not thumbnail.isNull() else None

        from PyQt6.QtGui import QFont, QFontMetrics
        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        self._font = font
        # 标题可用宽度：扣掉三色圆点区（~52）+ 窗口色圆点（~14）+ 右边距
        text_room = min(self.MAX_TEXT_W, self.WIDTH - 52 - 14 - 10)
        self._title = QFontMetrics(font).elidedText(
            title or '', Qt.TextElideMode.ElideMiddle, text_room)

        if self._thumb is not None:
            dpr = self._thumb.devicePixelRatio() or 1.0
            tw = self._thumb.width() / dpr
            th_ = self._thumb.height() / dpr
            body_h = int(th_ * self.WIDTH / max(1.0, tw))
            self._body_h = max(60, min(self.MAX_BODY_H, body_h))
        else:
            self._body_h = self.DEFAULT_BODY_H
        self._card_w = self.WIDTH
        self._card_h = self.TITLE_H + self._body_h
        m = self.SHADOW
        self.resize(self._card_w + 2 * m, self._card_h + 2 * m)

    def follow(self, cursor_pos: QPoint):
        """把迷你窗口放到光标右下方（扣掉阴影边，窗口本身落在 CURSOR_OFFSET 处）。"""
        m = self.SHADOW
        self.move(cursor_pos + self.CURSOR_OFFSET - QPoint(m, m))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        m = self.SHADOW
        card = QRectF(m, m, self._card_w, self._card_h)
        # 柔和阴影：由外向内一圈圈加深、略向下偏
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(m, 0, -1):
            a = int(40 * (1 - i / m) ** 2) + 2
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(card.adjusted(-i, -i + 3, i, i + 3),
                              self.RADIUS + i, self.RADIUS + i)
        # 整体圆角裁切：标题栏 + 内容区
        clip = QPainterPath()
        clip.addRoundedRect(card, self.RADIUS, self.RADIUS)
        p.save()
        p.setClipPath(clip)
        p.fillRect(card, self._body_bg)
        title = QRectF(card.left(), card.top(), card.width(), self.TITLE_H)
        p.fillRect(title, self._title_bg)
        body = QRectF(card.left(), card.top() + self.TITLE_H, card.width(), self._body_h)
        if self._thumb is not None:
            # 缩略图按宽度铺满，超高部分从顶部截取（看得到窗口上半部分）
            src_w = self._thumb.width()
            src_h = min(self._thumb.height(), src_w * body.height() / body.width())
            p.drawPixmap(body, self._thumb, QRectF(0, 0, src_w, src_h))
        else:
            # 占位：几行长短不一的淡色「文字线」，像一个终端窗口
            line = QColor(self._dim)
            line.setAlpha(70)
            p.setBrush(line)
            y = body.top() + 14
            for frac in (0.62, 0.45, 0.78, 0.34, 0.55, 0.7):
                if y + 4 > body.bottom() - 8:
                    break
                p.drawRoundedRect(QRectF(body.left() + 12, y, (body.width() - 24) * frac, 4), 2, 2)
                y += 14
        # 标题栏与内容区之间的分隔线
        sep = QColor(0, 0, 0, 60)
        p.fillRect(QRectF(card.left(), body.top() - 1, card.width(), 1), sep)
        p.restore()
        # 红黄绿三个圆点
        cy = card.top() + self.TITLE_H / 2
        for i, c in enumerate(('#ff5f57', '#febc2e', '#28c840')):
            p.setBrush(QColor(c))
            p.drawEllipse(QPointF(card.left() + 12 + i * 14, cy), 4.5, 4.5)
        # 窗口色圆点 + 标题
        x = card.left() + 12 + 3 * 14 + 4
        p.setBrush(self._dot)
        p.drawEllipse(QPointF(x + 4, cy), 3.5, 3.5)
        p.setPen(self._fg)
        p.setFont(self._font)
        p.drawText(QRectF(x + 12, card.top(), card.right() - (x + 12) - 8, self.TITLE_H),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self._title)
        # 描边
        border = QColor(self._accent)
        border.setAlpha(150)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), self.RADIUS, self.RADIUS)
        p.end()


class _ToolbarCheckBox(QCheckBox):
    """工具栏复选框：去掉 QCheckBox 固有的右侧空白，使其外框紧贴“指示器+文字”，
    与 QPushButton 一样填满自身外框。这样组与组之间的分隔线左右间距才会对称
    （否则复选框尾随的空白会让其后的分隔线显得偏左）。

    在紧贴的基础上再追加一点固定尾随间距（trail_gap），让相邻的多个复选框之间
    不至于挤在一起；默认 8px。"""

    def __init__(self, *args, trail_gap=8, **kwargs):
        super().__init__(*args, **kwargs)
        self._trail_gap = trail_gap

    def sizeHint(self):
        s = super().sizeHint()
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        # 样式给出的文字区域起点（指示器 + 间距之后）
        contents = self.style().subElementRect(
            QStyle.SubElement.SE_CheckBoxContents, opt, self)
        text_w = self.fontMetrics().horizontalAdvance(self.text())
        # 文字实际宽度（horizontalAdvance 含右侧 bearing，故 -2 不会裁剪字形），
        # 再加上固定尾随间距，避免相邻复选框互相紧贴
        want = contents.x() + text_w - 2
        if want > 0:
            s.setWidth(want + self._trail_gap)
        return s


class _FlowSeparator(QWidget):
    """流式布局中的垂直分隔符，外观与 QToolBar::separator 一致：
    宽度 12px（线居中），配合 FlowLayout 的 h_spacing=5 形成左右各约 11px 的对称间距。
    画一条 1px 实线，颜色随主题更新（见 set_line_color）。"""

    def __init__(self, parent=None, color="#3d3d5c"):
        super().__init__(parent)
        self.setObjectName("_flow_separator")
        self.setFixedWidth(2)  # 线居中，两侧由 FlowLayout 的 h_spacing 提供对称间距
        self.setFixedHeight(22)
        self._color = QColor(color)

    def set_line_color(self, color):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(self._color)
        pen.setWidth(1)
        p.setPen(pen)
        x = self.width() // 2
        p.drawLine(x, 4, x, self.height() - 4)
        p.end()


class _NavResizeHandle(QWidget):
    """内嵌导航面板与下方 Explorer/Git/Remote 之间的可拖拽分隔条：
    上下拖动改变导航列表高度（即调整二者相对高度）。

    外观仿 Git graph 上方的 QSplitter 分隔条：中间画一条带圆角的「抓手」
    （默认 #3d3d5c，hover/拖拽时高亮 #667eea），比单纯一条细线更易识别可拖拽。"""

    _IDLE = "#3d3d5c"
    _ACTIVE = "#667eea"

    def set_colors(self, idle: str, active: str):
        """按主题下发抓手颜色（idle=边框色，active=强调色）。"""
        if idle:
            self._IDLE = idle
        if active:
            self._ACTIVE = active
        self.update()

    def __init__(self, on_drag, on_release, parent=None):
        super().__init__(parent)
        self._on_drag = on_drag          # callback(delta_y:int)：实时调整高度
        self._on_release = on_release    # callback()：拖拽结束后落盘
        self.setFixedHeight(10)
        # 用 QSplitter 同款「上下分隔条」光标，悬停时一眼能看出可拖拽
        self.setCursor(Qt.CursorShape.SplitVCursor)
        self._press_y = None
        self._hover = False

    def _color(self):
        return self._ACTIVE if (self._hover or self._press_y is not None) else self._IDLE

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        # 居中画一条圆角抓手，左右各留 8px 边距（与 Git 分隔条 margin 一致）
        grip_h = 4
        x = 8
        y = (h - grip_h) / 2
        rect = QRectF(x, y, max(0, w - 2 * x), grip_h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._color()))
        painter.drawRoundedRect(rect, grip_h / 2, grip_h / 2)

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        if self._press_y is None:
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_y = event.globalPosition().y()
            self.update()

    def mouseMoveEvent(self, event):
        if self._press_y is not None:
            y = event.globalPosition().y()
            delta = int(y - self._press_y)
            if delta:
                self._press_y = y
                self._on_drag(delta)

    def mouseReleaseEvent(self, event):
        if self._press_y is not None:
            self._press_y = None
            self.update()
            self._on_release()
