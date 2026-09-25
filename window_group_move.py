"""「整组搬家」拖拽把手：抓住它一次拖动导航列表里的全部窗口。

场景：笔记本接上大显示器后，把一屏的终端窗口整体挪过去，不用逐个拖。
- 拖动过程中各窗口按同一位移刚性平移（保持相对布局）；
- 松手时若光标落在另一块屏幕上，按「源屏可用区 → 目标屏可用区」等比
  重排每个窗口的位置与尺寸（小屏到大屏自动放大），最大化窗口在新屏重新最大化；
- 全屏窗口（macOS 独立 Space）搬不动，跳过。
不用把手时，窗口标题栏照常单独拖动，互不影响。
"""
import sys

from PyQt6 import sip
from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QWidget

from app_logging import get_logger

logger = get_logger(__name__)


def map_rect_between(rect: QRect, src: QRect, dst: QRect) -> QRect:
    """把 rect 按它在 src 里的相对位置/占比，等比映射到 dst，并收进 dst 内。"""
    if src.width() <= 0 or src.height() <= 0:
        return QRect(rect)
    sx = dst.width() / src.width()
    sy = dst.height() / src.height()
    w = max(1, min(dst.width(), round(rect.width() * sx)))
    h = max(1, min(dst.height(), round(rect.height() * sy)))
    x = dst.left() + round((rect.left() - src.left()) * sx)
    y = dst.top() + round((rect.top() - src.top()) * sy)
    x = max(dst.left(), min(x, dst.left() + dst.width() - w))
    y = max(dst.top(), min(y, dst.top() + dst.height() - h))
    return QRect(x, y, w, h)


_mac_cg = None


def _mac_input_state():
    """(左键按着, Esc 按着)：直接问 CoreGraphics 的物理输入状态。"""
    global _mac_cg
    import ctypes
    if _mac_cg is None:
        cg = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        cg.CGEventSourceButtonState.restype = ctypes.c_bool
        cg.CGEventSourceButtonState.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        cg.CGEventSourceKeyState.restype = ctypes.c_bool
        cg.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
        _mac_cg = cg
    # 0 = kCGEventSourceStateCombinedSessionState；0 = 左键；53 = kVK_Escape
    return (_mac_cg.CGEventSourceButtonState(0, 0),
            _mac_cg.CGEventSourceKeyState(0, 53))


def _win_input_state():
    import ctypes
    user32 = ctypes.windll.user32
    # GetAsyncKeyState 看物理按键；左右键互换时物理左键对应 VK_RBUTTON
    vk_primary = 0x02 if user32.GetSystemMetrics(23) else 0x01  # SM_SWAPBUTTON
    return (bool(user32.GetAsyncKeyState(vk_primary) & 0x8000),
            bool(user32.GetAsyncKeyState(0x1B) & 0x8000))  # VK_ESCAPE


def drag_cancelled() -> bool:
    """QDrag.exec 刚返回时判断：是按 Esc 取消，还是真的松手放下。

    拖到应用外松手与按 Esc 取消，Qt 都只报 IgnoreAction，分不出来。
    区别在鼠标：Esc 取消时用户手还按着左键，松手放下时左键已抬起；
    再兼看 Esc 此刻是否按着兜底。macOS/Windows 原生拖拽期间 Qt 收不到
    鼠标/按键事件，只能直接问系统；其它平台由 Qt 自己跑拖拽循环，看 Qt 的按键状态。
    """
    try:
        if sys.platform == 'darwin':
            button_down, esc_down = _mac_input_state()
        elif sys.platform == 'win32':
            button_down, esc_down = _win_input_state()
        else:
            from PyQt6.QtGui import QGuiApplication
            button_down = bool(QGuiApplication.mouseButtons() & Qt.MouseButton.LeftButton)
            esc_down = False
        return bool(button_down or esc_down)
    except Exception:
        logger.debug("drag_cancelled: input state unavailable", exc_info=True)
        return False


def _screen_of(window):
    try:
        s = window.screen()
        if s is not None:
            return s
    except Exception:
        pass
    return QApplication.screenAt(window.frameGeometry().center())


class GroupMoveSession:
    """一次整组拖动：begin 快照各窗口原位，drag 刚性平移，finish 跨屏重排。"""

    def __init__(self, windows, start_global: QPoint):
        self.start = QPoint(start_global)
        self.items = []  # [(window, 原 pos, 原 geometry, 原屏幕, 是否最大化)]
        seen = set()
        for w in windows:
            if w is None or id(w) in seen:
                continue
            seen.add(id(w))
            try:
                if sip.isdeleted(w) or not w.isVisible():
                    continue
                if w.isFullScreen() or w.isMinimized():
                    continue
                self.items.append((w, w.pos(), w.geometry(), _screen_of(w),
                                   w.isMaximized()))
            except Exception:
                logger.debug("GroupMoveSession: skip window", exc_info=True)

    def _alive(self):
        for it in self.items:
            if not sip.isdeleted(it[0]):
                yield it

    def drag_to(self, global_pos: QPoint):
        delta = global_pos - self.start
        for w, pos, _geo, _scr, _mx in self._alive():
            w.move(pos + delta)

    def finish(self, global_pos: QPoint):
        """松手：光标落在别的屏幕上 → 等比重排到该屏；否则保持平移结果。"""
        target = QApplication.screenAt(global_pos)
        if target is not None:
            self.relayout_to(target)

    def relayout_to(self, target, anchor: QPoint = None):
        """把不在 target 上的窗口按原屏 → target 可用区等比重排。

        anchor：单窗口拖出列表时的松手点——窗口顶边中点对准它（仍收在屏内），
        让窗口落在鼠标放下的地方；不给则保持等比映射的相对位置。
        返回实际搬动的窗口数。
        """
        dst = target.availableGeometry()
        placed = []
        for w, _pos, geo, src_screen, was_max in self._alive():
            if src_screen is None or src_screen is target:
                continue
            new_geo = map_rect_between(geo, src_screen.availableGeometry(), dst)
            if anchor is not None and not was_max:
                new_geo.moveTopLeft(QPoint(anchor.x() - new_geo.width() // 2,
                                           anchor.y() - 10))
                new_geo = map_rect_between(new_geo, dst, dst)  # 收回屏内
            try:
                if was_max:
                    # 先回到普通状态落到新屏，再在新屏最大化
                    w.showNormal()
                w.setGeometry(new_geo)
                if was_max:
                    w.showMaximized()
                placed.append((w, new_geo, was_max))
            except Exception:
                logger.debug("GroupMoveSession.relayout_to: suppressed", exc_info=True)
        if placed:
            # 台前调度等系统策略可能在落位后把窗口推挪/压窄：稍后校正一次
            QTimer.singleShot(350, lambda: self._reassert(placed))
        return len(placed)

    @staticmethod
    def _reassert(placed):
        for w, geo, was_max in placed:
            try:
                if sip.isdeleted(w) or was_max or w.geometry() == geo:
                    continue
                # Qt 几何缓存可能与原生窗口脱节，先制造一次真实变化再设回
                w.resize(geo.width(), max(1, geo.height() - 1))
                w.setGeometry(geo)
            except Exception:
                logger.debug("GroupMoveSession._reassert: suppressed", exc_info=True)


def move_windows_to_screen(windows, target, anchor: QPoint = None) -> int:
    """把窗口搬到 target 屏幕（等比缩放；给 anchor 时落在该点）。返回搬动数。"""
    return GroupMoveSession(windows, QPoint()).relayout_to(target, anchor)


class GroupMoveGrip(QWidget):
    """画成 2×3 圆点的拖拽把手（矢量绘制，避免字形在 macOS 被渲染成 emoji）。

    windows_provider: 无参回调，返回按下时要一起移动的窗口列表。
    """

    moved = pyqtSignal()  # 一次整组拖动结束

    def __init__(self, windows_provider, parent=None):
        super().__init__(parent)
        self._provider = windows_provider
        self._session = None
        self._color = QColor('#888888')
        self._hover_color = QColor('#667eea')
        self._hover = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFixedSize(22, 20)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_colors(self, normal: str, hover: str):
        self._color = QColor(normal)
        self._hover_color = QColor(hover)
        self.update()

    def sizeHint(self):
        return QSize(22, 20)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self._hover or self._session is not None
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._hover_color if active else self._color)
        r = 1.6
        cx0 = self.width() / 2 - 3
        cy0 = self.height() / 2 - 5
        for col in range(2):
            for row in range(3):
                p.drawEllipse(QPoint(round(cx0 + col * 6), round(cy0 + row * 5)).toPointF(), r, r)
        p.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        try:
            windows = list(self._provider() or [])
        except Exception:
            logger.debug("GroupMoveGrip: provider failed", exc_info=True)
            windows = []
        self._session = GroupMoveSession(windows, event.globalPosition().toPoint())
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._session is None:
            return super().mouseMoveEvent(event)
        self._session.drag_to(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._session is None or event.button() != Qt.MouseButton.LeftButton:
            return super().mouseReleaseEvent(event)
        session, self._session = self._session, None
        # 把手本身随宿主窗口一起移动，光标位置一律用全局坐标
        session.finish(event.globalPosition().toPoint())
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()
        self.moved.emit()
        event.accept()
