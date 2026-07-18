"""
Git 面板 UI 组件
提供类似 Cursor IDE 的 Git 管理界面
"""
import json
import math
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QComboBox, QScrollArea, QListWidget,
    QListWidgetItem, QPlainTextEdit, QSizePolicy,
    QAbstractItemView, QMessageBox, QDialog, QTextEdit, QSplitter,
    QLineEdit, QDialogButtonBox, QMenu, QInputDialog
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QSize, QThread, QTimer, QPoint, QRect, QPointF, QRectF,
    QEvent,
)
from PyQt6.QtGui import (
    QFont, QColor, QBrush, QTextCursor, QTextCharFormat, QTextBlockFormat,
    QFontMetrics, QPainter, QPen, QAction, QIcon, QPixmap, QPainterPath
)


def _make_git_tool_icon(kind: str, color: str, px: int = 16) -> QIcon:
    """用 QPainter 画出统一风格的线条图标（刷新/加号/齿轮）。

    三者用同一支笔（同线宽、圆角端点）、同一画布尺寸绘制并居中，因此大小、
    粗细、颜色、对齐完全一致——解决直接用 ⟳ / + / ⚙ 字形时大小粗细参差、
    且 ⚙ 在 macOS 上被渲染成彩色 emoji 的问题。
    """
    scale = 3  # 超采样，缩回 px 时边缘更锐利
    s = px * scale
    pm = QPixmap(s, s)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.5 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    cx = cy = s / 2.0

    if kind == 'plus':
        m = s * 0.28
        p.drawLine(QPointF(cx, cy - m), QPointF(cx, cy + m))
        p.drawLine(QPointF(cx - m, cy), QPointF(cx + m, cy))

    elif kind == 'close':
        m = s * 0.24
        p.drawLine(QPointF(cx - m, cy - m), QPointF(cx + m, cy + m))
        p.drawLine(QPointF(cx + m, cy - m), QPointF(cx - m, cy + m))

    elif kind == 'up':
        m = s * 0.26
        p.drawLine(QPointF(cx, cy - m), QPointF(cx, cy + m))  # 竖杆
        head = s * 0.17                                       # 箭头
        p.drawLine(QPointF(cx, cy - m), QPointF(cx - head, cy - m + head))
        p.drawLine(QPointF(cx, cy - m), QPointF(cx + head, cy - m + head))

    elif kind == 'home':
        apex = QPointF(cx, cy - s * 0.30)        # 屋脊
        eave_y = cy - s * 0.02                    # 屋檐高度
        p.drawLine(QPointF(cx - s * 0.32, eave_y), apex)   # 左斜屋顶（带出檐）
        p.drawLine(apex, QPointF(cx + s * 0.32, eave_y))   # 右斜屋顶
        bw = s * 0.22                             # 墙体半宽
        bottom = cy + s * 0.28
        p.drawLine(QPointF(cx - bw, eave_y), QPointF(cx - bw, bottom))
        p.drawLine(QPointF(cx + bw, eave_y), QPointF(cx + bw, bottom))
        p.drawLine(QPointF(cx - bw, bottom), QPointF(cx + bw, bottom))

    elif kind in ('star', 'star_filled'):
        r_out = s * 0.34
        r_in = r_out * 0.42
        path = QPainterPath()
        for i in range(10):
            ang = -math.pi / 2 + i * (math.pi / 5)
            rr = r_out if i % 2 == 0 else r_in
            x, y = cx + rr * math.cos(ang), cy + rr * math.sin(ang)
            path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
        path.closeSubpath()
        if kind == 'star_filled':
            p.setBrush(QColor(color))   # 已收藏 → 实心
        p.drawPath(path)

    elif kind == 'refresh':
        r = s * 0.29
        rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
        start_deg, span_deg = 60.0, 285.0
        p.drawArc(rect, int(start_deg * 16), int(span_deg * 16))
        # 在弧末端补一个箭头（两根短斜线组成的尖角）
        end = math.radians(start_deg + span_deg)
        ex, ey = cx + r * math.cos(end), cy - r * math.sin(end)
        tx, ty = -math.sin(end), -math.cos(end)  # 逆时针切线方向
        bl = s * 0.22
        for a in (math.radians(145), math.radians(-145)):
            ca, sa = math.cos(a), math.sin(a)
            p.drawLine(QPointF(ex, ey),
                       QPointF(ex + (tx * ca - ty * sa) * bl,
                               ey + (tx * sa + ty * ca) * bl))

    elif kind == 'caret_down':
        # 向下的箭头（下拉指示），简洁清晰
        w = s * 0.22
        h = s * 0.12
        p.drawLine(QPointF(cx - w, cy - h), QPointF(cx, cy + h))
        p.drawLine(QPointF(cx, cy + h), QPointF(cx + w, cy - h))

    elif kind == 'list':
        # 三条横线：列表 / 返回主机列表
        m = s * 0.26
        for dy in (-m, 0.0, m):
            p.drawLine(QPointF(cx - m, cy + dy), QPointF(cx + m, cy + dy))

    elif kind == 'stash':
        # 收纳箱：箱体 + 上盖分隔线 + 提手，表示"贮藏改动"
        w2, h2 = s * 0.30, s * 0.26
        p.drawRoundedRect(QRectF(cx - w2, cy - h2, 2 * w2, 2 * h2),
                          s * 0.05, s * 0.05)
        lid_y = cy - h2 + s * 0.16
        p.drawLine(QPointF(cx - w2, lid_y), QPointF(cx + w2, lid_y))
        hw = s * 0.10
        p.drawLine(QPointF(cx - hw, lid_y + s * 0.12),
                   QPointF(cx + hw, lid_y + s * 0.12))

    elif kind == 'gear':
        # 经典平顶齿 cog：每个齿用 内→外→外→内 四个顶点，齿顶是平的
        teeth = 8
        r_out, r_in = s * 0.36, s * 0.27
        tooth_w = 0.5  # 齿顶占每齿周期的比例
        period = 2 * math.pi / teeth
        path = QPainterPath()
        for i in range(teeth):
            base = i * period - math.pi / 2  # 从正上方开始
            a0, a1 = base, base + period * tooth_w
            for j, (ang, rr) in enumerate((
                (a0, r_in), (a0, r_out), (a1, r_out), (a1, r_in),
            )):
                x, y = cx + rr * math.cos(ang), cy + rr * math.sin(ang)
                if i == 0 and j == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
        path.closeSubpath()
        p.drawPath(path)
        hole = s * 0.13
        p.drawEllipse(QPointF(cx, cy), hole, hole)

    p.end()
    return QIcon(pm)

from git_manager import GitManager, GitFile, FileStatus
from i18n import t, get_language
import app_config
from app_logging import get_logger

logger = get_logger(__name__)


# 文件状态颜色
STATUS_COLORS = {
    FileStatus.MODIFIED: '#e5c07b',     # 黄色
    FileStatus.ADDED: '#98c379',        # 绿色
    FileStatus.DELETED: '#e06c75',      # 红色
    FileStatus.RENAMED: '#61afef',      # 蓝色
    FileStatus.COPIED: '#61afef',       # 蓝色
    FileStatus.UNTRACKED: '#98c379',    # 绿色
    FileStatus.UNMERGED: '#e06c75',     # 红色
}

# 文件状态图标
STATUS_ICONS = {
    FileStatus.MODIFIED: 'M',
    FileStatus.ADDED: 'A',
    FileStatus.DELETED: 'D',
    FileStatus.RENAMED: 'R',
    FileStatus.COPIED: 'C',
    FileStatus.UNTRACKED: 'U',
    FileStatus.UNMERGED: '!',
}


def _mono_font(size: int = 12, *, pixel: bool = False) -> QFont:
    """跨平台等宽字体。

    Menlo 只在 macOS 存在；Windows 上每次 QFont("Menlo") 都要走一遍
    字体回退/别名查询。用 setFamilies 给出各平台首选，命中即停。

    pixel=True 时按像素设字号（与 UI 其余处的 QSS `font-size: Npx` 对齐）；
    默认按 point 设字号。point 在多数屏上比同数值的 px 视觉更大。
    """
    f = QFont()
    f.setFamilies(["Menlo", "Consolas", "Cascadia Mono",
                   "DejaVu Sans Mono", "Courier New"])
    if pixel:
        f.setPixelSize(size)
    else:
        f.setPointSize(size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    return f


class GitFileItem(QWidget):
    """Git 文件列表项"""

    # 信号
    stage_clicked = pyqtSignal(str)         # 暂存按钮点击
    unstage_clicked = pyqtSignal(str)       # 取消暂存按钮点击
    discard_clicked = pyqtSignal(str)       # 放弃更改按钮点击
    diff_clicked = pyqtSignal(str, bool)    # 查看 diff（路径, 是否暂存区）
    resolve_ours_clicked = pyqtSignal(str)      # 冲突：采用我方版本
    resolve_theirs_clicked = pyqtSignal(str)    # 冲突：采用对方版本
    mark_resolved_clicked = pyqtSignal(str)     # 冲突：标记为已解决（git add）

    def __init__(self, git_file: GitFile, is_staged: bool = False, theme: dict = None, parent=None):
        super().__init__(parent)
        self.git_file = git_file
        self.is_staged = is_staged
        self.theme = theme or {}

        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # 状态标签（冲突文件用醒目的 "!!" 红色标记）
        is_conflict = getattr(self.git_file, 'is_conflict', False)
        status_icon = '!!' if is_conflict else STATUS_ICONS.get(self.git_file.status, '?')
        status_color = '#e06c75' if is_conflict else STATUS_COLORS.get(self.git_file.status, '#888')

        self.status_label = QLabel(status_icon)
        self.status_label.setFixedWidth(20)
        self.status_label.setStyleSheet(f"""
            QLabel {{
                color: {status_color};
                font-weight: bold;
                font-family: monospace;
            }}
        """)
        layout.addWidget(self.status_label)

        # 文件名（冲突文件整行红色加粗，确保一眼可见）
        filename_text = self.git_file.path
        if is_conflict:
            filename_text += f"  ({t('git.conflict_suffix')})"
        self.filename_label = QLabel(filename_text)
        # 让文件名可被压缩到很窄：路径很长时优先保住分区/行尾的操作按钮（+/- 等），
        # 文件名用省略号收尾，而不是把整行撑得比可视区还宽、把右侧按钮顶出去看不见
        # （这正是分区标题 title_label 已采用的同一套办法）。中间省略保留目录头与文件名尾。
        self._filename_text = filename_text
        self.filename_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.filename_label.setMinimumWidth(0)
        self.filename_label.setToolTip(self.git_file.path)  # 完整路径仍可悬停查看
        if is_conflict:
            self.filename_label.setStyleSheet("""
                QLabel {
                    color: #e06c75;
                    font-weight: bold;
                }
            """)
        else:
            self.filename_label.setStyleSheet(f"""
                QLabel {{
                    color: {self.theme.get('text', '#eaeaea')};
                }}
            """)
        layout.addWidget(self.filename_label, 1)
        # 宽度随侧栏变化时用省略号重算可见文字（…），避免把字硬裁成半个
        self.filename_label.resizeEvent = self._elide_filename

        # 操作按钮容器
        self.btn_container = QWidget()
        btn_layout = QHBoxLayout(self.btn_container)
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.setSpacing(4)

        btn_style = f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 3px;
                padding: 2px 6px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """

        if self.is_staged:
            # 取消暂存按钮
            self.unstage_btn = QPushButton("-")
            self.unstage_btn.setToolTip(t("git.unstage_tooltip"))
            self.unstage_btn.setFixedSize(24, 20)
            self.unstage_btn.setStyleSheet(btn_style)
            self.unstage_btn.clicked.connect(lambda: self.unstage_clicked.emit(self.git_file.path))
            btn_layout.addWidget(self.unstage_btn)
        else:
            # 暂存按钮
            self.stage_btn = QPushButton("+")
            self.stage_btn.setToolTip(t("git.stage_tooltip"))
            self.stage_btn.setFixedSize(24, 20)
            self.stage_btn.setStyleSheet(btn_style)
            self.stage_btn.clicked.connect(lambda: self.stage_clicked.emit(self.git_file.path))
            btn_layout.addWidget(self.stage_btn)


        # 初始隐藏按钮
        self.btn_container.setVisible(False)
        layout.addWidget(self.btn_container)

        # 设置鼠标跟踪
        self.setMouseTracking(True)

    def _elide_filename(self, event=None):
        """按当前标签宽度对文件路径做中间省略（保留目录头与文件名尾）。"""
        fm = self.filename_label.fontMetrics()
        elided = fm.elidedText(
            self._filename_text, Qt.TextElideMode.ElideMiddle, self.filename_label.width()
        )
        # 直接 setText 才能让 QLabel 真正显示省略后的文字（宽度未变，不触发递归）
        QLabel.setText(self.filename_label, elided)

    def enterEvent(self, event):
        """鼠标进入"""
        self.btn_container.setVisible(True)
        bg_color = self.theme.get('bg_hover', '#4d4d6c')
        self.setStyleSheet(f"background-color: {bg_color};")
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开"""
        self.btn_container.setVisible(False)
        self.setStyleSheet("")
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击查看 diff"""
        try:
            self.diff_clicked.emit(self.git_file.path, self.is_staged)
            super().mouseDoubleClickEvent(event)
        except RuntimeError:
            # 条目可能在刷新中已被销毁，忽略本次事件即可
            pass

    def contextMenuEvent(self, event):
        """右键菜单：暂存 / 取消暂存 / 丢弃更改，避免常驻按钮误触"""
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #252526; color: #cccccc;"
            " border: 1px solid #454545; }"
            "QMenu::item { padding: 6px 24px 6px 12px; }"
            "QMenu::item:selected { background-color: #094771; }"
        )
        if getattr(self.git_file, 'is_conflict', False):
            # 冲突文件：整体采用一方版本 / 手工改完后标记已解决
            ours_act = menu.addAction(t("git.menu_resolve_ours"))
            theirs_act = menu.addAction(t("git.menu_resolve_theirs"))
            menu.addSeparator()
            resolved_act = menu.addAction(t("git.menu_mark_resolved"))
            chosen = menu.exec(event.globalPos())
            if chosen == ours_act:
                self.resolve_ours_clicked.emit(self.git_file.path)
            elif chosen == theirs_act:
                self.resolve_theirs_clicked.emit(self.git_file.path)
            elif chosen == resolved_act:
                self.mark_resolved_clicked.emit(self.git_file.path)
        elif self.is_staged:
            unstage_act = menu.addAction(t("git.menu_unstage"))
            chosen = menu.exec(event.globalPos())
            if chosen == unstage_act:
                self.unstage_clicked.emit(self.git_file.path)
        else:
            stage_act = menu.addAction(t("git.menu_stage"))
            menu.addSeparator()
            discard_act = menu.addAction(t("git.menu_discard"))
            chosen = menu.exec(event.globalPos())
            if chosen == stage_act:
                self.stage_clicked.emit(self.git_file.path)
            elif chosen == discard_act:
                self.discard_clicked.emit(self.git_file.path)


class CollapsibleSection(QWidget):
    """可折叠的分区"""

    def __init__(self, title: str, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._is_expanded = True

        self._setup_ui(title)

    def _setup_ui(self, title: str):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        self.header = QFrame()
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(8, 6, 8, 6)

        # 展开/折叠图标
        self.toggle_icon = QLabel("▼")
        self.toggle_icon.setFixedWidth(16)
        header_layout.addWidget(self.toggle_icon)

        # 标题
        self._title_text = title
        self.title_label = QLabel(title)
        # 让标题可被压缩到很窄：侧栏过窄时优先保住右侧操作按钮，标题文字用省略号收尾，
        # 而不是把固定尺寸的按钮容器挤出可视区（设置键在另一布局里，不受此影响）。
        self.title_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.title_label.setMinimumWidth(0)
        header_layout.addWidget(self.title_label, 1)
        # 标题宽度随侧栏变化时，用省略号重算可见文字（…），避免单纯裁剪把字截成半个
        self.title_label.resizeEvent = self._elide_title

        header_layout.addStretch(0)

        # 操作按钮容器
        self.action_container = QWidget()
        self.action_layout = QHBoxLayout(self.action_container)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(4)
        header_layout.addWidget(self.action_container)

        layout.addWidget(self.header)

        # 内容区域
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        layout.addWidget(self.content)

        # 点击标题栏折叠/展开
        self.header.mousePressEvent = self._toggle

        self._update_style()

    def _toggle(self, event=None):
        """切换折叠状态"""
        self._is_expanded = not self._is_expanded
        self.content.setVisible(self._is_expanded)
        self.toggle_icon.setText("▼" if self._is_expanded else "▶")

    def _update_style(self):
        """更新样式（标题与操作按钮也在此重设，保证切主题后不残留旧配色）"""
        self.header.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)
        self.toggle_icon.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text_dim', '#888')};
            }}
        """)
        self.title_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text', '#eaeaea')};
                font-weight: bold;
                font-size: 12px;
            }}
        """)
        for i in range(self.action_layout.count()):
            w = self.action_layout.itemAt(i).widget()
            if isinstance(w, QPushButton):
                w.setStyleSheet(self._action_btn_qss())

    def _elide_title(self, event=None):
        """按当前标签宽度对标题做省略号处理（窄到放不下时显示 'Staged Cha…'）"""
        fm = self.title_label.fontMetrics()
        elided = fm.elidedText(
            self._title_text, Qt.TextElideMode.ElideRight, self.title_label.width()
        )
        # 直接 setText 才能让 QLabel 真正显示省略后的文字（不触发递归：宽度未变）
        QLabel.setText(self.title_label, elided)

    def set_title(self, title: str):
        """设置标题"""
        self._title_text = title
        self._elide_title()

    def _action_btn_qss(self) -> str:
        """操作按钮的主题样式（新增按钮与切主题重刷共用）"""
        return f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 3px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """

    def add_action_button(self, text: str, tooltip: str = "") -> QPushButton:
        """添加操作按钮"""
        btn = QPushButton(text)
        btn.setToolTip(tooltip)
        btn.setFixedSize(24, 20)
        btn.setStyleSheet(self._action_btn_qss())
        self.action_layout.addWidget(btn)
        return btn

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()


class GitChangesWidget(QScrollArea):
    """Git 变更列表组件"""

    # 信号
    stage_file = pyqtSignal(str)
    unstage_file = pyqtSignal(str)
    discard_file = pyqtSignal(str)
    stage_all = pyqtSignal()
    unstage_all = pyqtSignal()
    view_diff = pyqtSignal(str, bool)
    resolve_ours = pyqtSignal(str)      # 冲突：采用我方版本
    resolve_theirs = pyqtSignal(str)    # 冲突：采用对方版本
    mark_resolved = pyqtSignal(str)     # 冲突：标记为已解决

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
        """)

        # 内容容器
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 已暂存区
        self.staged_section = CollapsibleSection(t("git.staged_changes", n=0), self.theme)
        self.unstage_all_btn = self.staged_section.add_action_button("-", t("git.unstage_all_tooltip"))
        self.unstage_all_btn.clicked.connect(self.unstage_all.emit)

        self.staged_list = QWidget()
        self.staged_layout = QVBoxLayout(self.staged_list)
        self.staged_layout.setContentsMargins(0, 0, 0, 0)
        self.staged_layout.setSpacing(0)
        self.staged_section.content_layout.addWidget(self.staged_list)
        layout.addWidget(self.staged_section)

        # 未暂存区
        self.unstaged_section = CollapsibleSection(t("git.changes", n=0), self.theme)
        self.stage_all_btn = self.unstaged_section.add_action_button("+", t("git.stage_all_tooltip"))
        self.stage_all_btn.clicked.connect(self.stage_all.emit)

        self.unstaged_list = QWidget()
        self.unstaged_layout = QVBoxLayout(self.unstaged_list)
        self.unstaged_layout.setContentsMargins(0, 0, 0, 0)
        self.unstaged_layout.setSpacing(0)
        self.unstaged_section.content_layout.addWidget(self.unstaged_list)
        layout.addWidget(self.unstaged_section)

        layout.addStretch()
        self.setWidget(content)

    def update_files(self, staged: list, unstaged: list):
        """更新文件列表"""
        # 列表没变就不重建：避免每次刷新（5s 定时 / fetch 后）都销毁重建条目，
        # 否则用户正在双击的条目可能在事件处理途中被删 → RuntimeError。
        fp = (
            tuple((f.path, getattr(f.status, 'value', f.status),
                   getattr(f, 'is_conflict', False)) for f in staged),
            tuple((f.path, getattr(f.status, 'value', f.status),
                   getattr(f, 'is_conflict', False)) for f in unstaged),
        )
        if fp == getattr(self, '_files_fingerprint', None):
            return
        self._files_fingerprint = fp

        # 清空现有列表
        self._clear_layout(self.staged_layout)
        self._clear_layout(self.unstaged_layout)

        # 更新已暂存文件
        self.staged_section.set_title(t("git.staged_changes", n=len(staged)))
        for git_file in staged:
            item = GitFileItem(git_file, is_staged=True, theme=self.theme)
            item.unstage_clicked.connect(self.unstage_file.emit)
            item.diff_clicked.connect(self.view_diff.emit)
            self.staged_layout.addWidget(item)

        # 更新未暂存文件
        self.unstaged_section.set_title(t("git.changes", n=len(unstaged)))
        for git_file in unstaged:
            item = GitFileItem(git_file, is_staged=False, theme=self.theme)
            item.stage_clicked.connect(self.stage_file.emit)
            item.discard_clicked.connect(self.discard_file.emit)
            item.diff_clicked.connect(self.view_diff.emit)
            item.resolve_ours_clicked.connect(self.resolve_ours.emit)
            item.resolve_theirs_clicked.connect(self.resolve_theirs.emit)
            item.mark_resolved_clicked.connect(self.mark_resolved.emit)
            self.unstaged_layout.addWidget(item)

    def _clear_layout(self, layout):
        """清空布局中的所有组件"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background-color: {theme.get('bg_dark', '#1a1a2e')};
            }}
        """)
        self.staged_section.apply_theme(theme)
        self.unstaged_section.apply_theme(theme)
        # 文件条目在 update_files 时按当时的主题创建；作废指纹，
        # 让下一次状态刷新用新主题重建条目（否则旧配色一直残留）
        self._files_fingerprint = None

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.staged_section.set_title(t("git.staged_changes", n=self.staged_layout.count()))
        self.unstage_all_btn.setToolTip(t("git.unstage_all_tooltip"))
        self.unstaged_section.set_title(t("git.changes", n=self.unstaged_layout.count()))
        self.stage_all_btn.setToolTip(t("git.stage_all_tooltip"))


class _CommitMessageWorker(QThread):
    """后台线程：调用 OpenAI 兼容的 /chat/completions，根据 diff 生成提交信息。

    放到独立线程里跑，避免网络请求阻塞 UI。结果通过信号回到 UI 线程。
    """
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, config: dict, diff: str, lang: str, parent=None):
        super().__init__(parent)
        self._config = config or {}
        self._diff = diff
        self._lang = lang

    def run(self):
        try:
            import requests
        except Exception as e:  # pragma: no cover - requests 是已有依赖
            self.failed.emit(str(e))
            return

        cfg = self._config
        base = (cfg.get('api_base') or 'https://api.openai.com/v1').rstrip('/')
        url = base + '/chat/completions'

        headers = {'Content-Type': 'application/json'}
        key = (cfg.get('api_key') or '').strip()
        if key:
            headers['Authorization'] = f'Bearer {key}'

        lang_line = ('请用简体中文写提交信息。' if self._lang == 'zh'
                     else 'Write the commit message in English.')
        system = (
            "You are an expert software engineer writing a git commit message. "
            "Follow the Conventional Commits style (feat:, fix:, refactor:, docs:, "
            "style:, test:, chore:). Keep the subject line under 72 characters. "
            "If the change is non-trivial, add a blank line then a short body with "
            "bullet points describing what changed and why. " + lang_line +
            " Respond with ONLY the commit message text — no markdown code fences, "
            "no preamble, no quotes, no explanations."
        )
        user = ("Here are the repository changes. Write a single commit message "
                "for them:\n\n" + self._diff)

        payload = {
            'model': cfg.get('model') or 'gpt-4',
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            'temperature': 0.3,
            'stream': False,
        }
        mt = cfg.get('max_tokens')
        if isinstance(mt, int) and mt > 0:
            payload['max_tokens'] = min(mt, 1024)  # 提交信息不需要太长

        proxies = None
        proxy = (cfg.get('proxy') or '').strip()
        if proxy:
            proxies = {'http': proxy, 'https': proxy}

        timeout = cfg.get('timeout') or 30

        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=timeout, proxies=proxies)
        except Exception as e:
            self.failed.emit(str(e))
            return

        if resp.status_code != 200:
            snippet = (resp.text or '')[:300]
            self.failed.emit(f"HTTP {resp.status_code}: {snippet}")
            return

        try:
            data = resp.json()
            content = data['choices'][0]['message']['content']
        except Exception as e:
            self.failed.emit(f"unexpected response: {e}")
            return

        self.succeeded.emit(self._clean(content))

    @staticmethod
    def _clean(text: str) -> str:
        """清洗模型输出：去掉推理模型的 <think>/<thinking> 块、``` 代码围栏和首尾空白。"""
        import re
        text = text or ''
        # 1) 去掉成对的推理块（DeepSeek/Qwen 等推理模型会内联在 content 里）
        text = re.sub(r'(?is)<think(?:ing)?>.*?</think(?:ing)?>', '', text)
        # 2) 有时只回传了闭合标签（推理在前、答案在后）→ 取最后一个闭合标签之后的内容
        for tag in ('</think>', '</thinking>'):
            if tag in text:
                text = text.rsplit(tag, 1)[-1]
        # 3) 残留的未闭合开标签
        text = re.sub(r'(?is)<think(?:ing)?>', '', text)
        text = text.strip()
        # 4) 去掉 ``` 代码围栏
        if text.startswith('```'):
            lines = text.splitlines()
            if lines and lines[0].startswith('```'):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith('```'):
                lines = lines[:-1]
            text = '\n'.join(lines).strip()
        return text


class _GitOpWorker(QThread):
    """后台执行 push/pull 等网络操作，避免阻塞 UI 线程（否则会卡死）。

    GitManager 内部通过 Qt 信号回报错误/状态，跨线程会自动排队到 UI 线程，
    所以在工作线程里直接调用是安全的。
    """
    done = pyqtSignal(bool, str)  # (success, kind)

    def __init__(self, fn, kind: str, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._kind = kind

    def run(self):
        try:
            ok = bool(self._fn())
        except Exception:
            logger.exception("git op '%s' failed", self._kind)
            ok = False
        self.done.emit(ok, self._kind)


class _RefreshWorker(QThread):
    """后台收集刷新面板所需的全部只读数据（status / branches / tags / log）。

    避免在 UI 线程跑 git status/branch/log —— 大仓库的 `git log -150 --all`
    会卡住界面。GitManager 的读命令各自 spawn/release 子进程（_proc_lock 守护），
    并发读安全，可与后台 fetch 同时运行。数据收齐后一次性发回 UI 线程再应用。
    """
    loaded = pyqtSignal(dict)

    def __init__(self, git_manager, log_limit: int = 150, parent=None):
        super().__init__(parent)
        self._gm = git_manager
        # 用户在 graph 里翻页加载过更多提交时，全量刷新按已加载条数重取，
        # 避免刷新把列表缩回第一页、丢掉滚动位置。
        self._log_limit = max(150, int(log_limit))

    def run(self):
        data = {'ok': False}
        try:
            gm = self._gm
            staged, unstaged = gm.get_status()
            data['staged'] = staged
            data['unstaged'] = unstaged
            data['ahead'], data['behind'] = gm.get_ahead_behind()
            data['branches'] = gm.get_branches()
            data['tags'] = gm.get_tags()
            data['head_ref'] = gm.get_head_ref()
            data['commits'] = gm.get_log(limit=self._log_limit, all_branches=True)
            data['log_limit'] = self._log_limit
            data['merging'] = gm.is_merging()
            data['ok'] = True
        except Exception:
            logger.exception("git refresh worker failed")
            data['ok'] = False
        self.loaded.emit(data)


class _LogPageWorker(QThread):
    """后台取提交历史的下一页（graph 滚动到底时增量加载用）。"""
    loaded = pyqtSignal(list, int)  # (commits, skip)

    def __init__(self, git_manager, skip: int, limit: int, parent=None):
        super().__init__(parent)
        self._gm = git_manager
        self._skip = skip
        self._limit = limit

    def run(self):
        try:
            commits = self._gm.get_log(limit=self._limit, all_branches=True,
                                       skip=self._skip)
        except Exception:
            logger.exception("git log page worker failed")
            commits = []
        self.loaded.emit(commits, self._skip)


class _StatusWorker(QThread):
    """后台收集轻量状态（status / ahead-behind / merge 态），供 5s 定时刷新使用。

    与 _RefreshWorker 的区别：不拉 branches/tags/log，开销小一个数量级。
    定时刷新原先在 UI 线程同步跑 3+ 个 git 子进程——macOS 上每个 ~2ms 无感,
    Windows 上每个 30-100ms（杀软扫描时更糟），表现为每 5 秒一次的明显卡顿。
    """
    loaded = pyqtSignal(dict)

    def __init__(self, git_manager, parent=None):
        super().__init__(parent)
        self._gm = git_manager

    def run(self):
        data = {'ok': False}
        try:
            gm = self._gm
            data['staged'], data['unstaged'] = gm.get_status()
            data['ahead'], data['behind'] = gm.get_ahead_behind()
            data['merging'] = gm.is_merging()
            data['ok'] = True
        except Exception:
            logger.exception("git status worker failed")
            data['ok'] = False
        self.loaded.emit(data)


class GitCommitWidget(QFrame):
    """Git 提交区组件"""

    # 信号
    commit_requested = pyqtSignal(str)
    push_requested = pyqtSignal()
    pull_requested = pyqtSignal()
    generate_requested = pyqtSignal()

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        # 领先/落后远程的提交数，以及 push/pull 是否进行中（决定按钮文案）
        self._ahead = 0
        self._behind = 0
        self._push_busy = False
        self._pull_busy = False
        self._commit_busy = False
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # 提交信息输入框（高度可随上方分隔条拖拽变化）
        self.message_input = QPlainTextEdit()
        self.message_input.setPlaceholderText(t("git.commit_placeholder"))
        self.message_input.setMinimumHeight(70)
        self.message_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.message_input.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 6px;
                font-size: 13px;
            }}
            QPlainTextEdit:focus {{
                border-color: {self.theme.get('accent', '#667eea')};
            }}
        """)
        layout.addWidget(self.message_input)

        # ✨ 用大模型生成提交信息
        self.generate_btn = QPushButton(t("git.generate_msg"))
        self.generate_btn.setToolTip(t("git.generate_msg_tooltip"))
        self.generate_btn.setStyleSheet(self._generate_btn_style(self.theme))
        self.generate_btn.clicked.connect(self._on_generate)
        layout.addWidget(self.generate_btn)

        # 提交按钮
        self.commit_btn = QPushButton("Commit")
        self.commit_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('accent', '#667eea')};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('accent_hover', '#7a8efa')};
            }}
            QPushButton:pressed {{
                background-color: {self.theme.get('accent_pressed', '#5a6fd6')};
            }}
            QPushButton:disabled {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text_dim', '#888')};
            }}
        """)
        self.commit_btn.clicked.connect(self._on_commit)
        layout.addWidget(self.commit_btn)

        # Push/Pull 按钮行
        sync_layout = QHBoxLayout()
        sync_layout.setSpacing(8)

        # Pull 按钮
        self.pull_btn = QPushButton("↓ Pull")
        self.pull_btn.setToolTip(t("git.pull_tooltip"))
        self.pull_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """)
        self.pull_btn.clicked.connect(self.pull_requested.emit)
        sync_layout.addWidget(self.pull_btn)

        # Push 按钮
        self.push_btn = QPushButton("↑ Push")
        self.push_btn.setToolTip(t("git.push_tooltip"))
        self.push_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('success', '#4ade80')};
                color: #000;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('success_hover', '#22c55e')};
            }}
        """)
        self.push_btn.clicked.connect(self.push_requested.emit)
        sync_layout.addWidget(self.push_btn)

        layout.addLayout(sync_layout)

        self._update_style()

    def _update_style(self):
        """更新样式"""
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
                border-top: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)

    def _on_commit(self):
        """提交按钮点击

        注意：这里不清空输入框 —— 提交可能失败（如无暂存改动、钩子拒绝），
        若在此处提前清空，用户辛苦写/生成的提交信息就丢了。改由 GitPanel
        在提交确实成功后调用 clear_message()。"""
        message = self.message_input.toPlainText().strip()
        if message:
            self.commit_requested.emit(message)

    def clear_message(self):
        """清空提交信息输入框（仅在提交成功后由 GitPanel 调用）。"""
        self.message_input.clear()

    def _on_generate(self):
        """生成提交信息按钮点击 —— 由 GitPanel 接管（它持有 GitManager 和 LLM 配置）"""
        self.generate_requested.emit()

    def set_generating(self, generating: bool):
        """切换生成中状态：禁用按钮并改文案，避免重复点击。"""
        self.generate_btn.setEnabled(not generating)
        self.generate_btn.setText(t("git.generating") if generating else t("git.generate_msg"))

    def set_message(self, text: str):
        """把生成的提交信息填进输入框。

        注意：生成是异步的，完成时用户很可能正在别处（如终端）打字，
        这里绝不能 setFocus()，否则会把光标从用户正在输入的地方抢走。
        只填文本，用户想编辑时自行点进来即可。"""
        self.message_input.setPlainText(text)

    def set_busy(self, kind: str, busy: bool):
        """push/pull/commit 进行中：禁用相关按钮并显示忙碌文案，避免重复点击。"""
        if kind == 'push':
            self._push_busy = busy
            self.push_btn.setEnabled(not busy)
        elif kind == 'pull':
            self._pull_busy = busy
            self.pull_btn.setEnabled(not busy)
        elif kind == 'commit':
            self._commit_busy = busy
            self.commit_btn.setEnabled(not busy)
            self.commit_btn.setText("Committing…" if busy else "Commit")
        self._update_sync_button_text()

    def set_ahead_behind(self, ahead: int, behind: int):
        """更新领先/落后远程的提交数，反映到 Push/Pull 按钮文案上。"""
        self._ahead = max(0, int(ahead))
        self._behind = max(0, int(behind))
        self._update_sync_button_text()

    def _update_sync_button_text(self):
        """按当前 ahead/behind 与忙碌状态刷新 Push/Pull 按钮文字。"""
        if self._push_busy:
            self.push_btn.setText("↑ Pushing…")
        else:
            self.push_btn.setText(f"↑ Push ({self._ahead})" if self._ahead else "↑ Push")
        if self._pull_busy:
            self.pull_btn.setText("↓ Pulling…")
        else:
            self.pull_btn.setText(f"↓ Pull ({self._behind})" if self._behind else "↓ Pull")

    @staticmethod
    def _generate_btn_style(theme: dict) -> str:
        return f"""
            QPushButton {{
                background-color: #7c3aed;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: #8b5cf6;
            }}
            QPushButton:pressed {{
                background-color: #6d28d9;
            }}
            QPushButton:disabled {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text_dim', '#888')};
            }}
        """

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()

        self.message_input.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {theme.get('bg_medium', '#16213e')};
                color: {theme.get('text', '#eaeaea')};
                border: 1px solid {theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 6px;
                font-size: 13px;
            }}
            QPlainTextEdit:focus {{
                border-color: {theme.get('accent', '#667eea')};
            }}
        """)

        self.commit_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('accent', '#667eea')};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {theme.get('accent_hover', '#7a8efa')};
            }}
            QPushButton:pressed {{
                background-color: {theme.get('accent_pressed', '#5a6fd6')};
            }}
            QPushButton:disabled {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text_dim', '#888')};
            }}
        """)

        self.pull_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """)

        self.push_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('success', '#4ade80')};
                color: #000;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {theme.get('success_hover', '#22c55e')};
            }}
        """)

        self.generate_btn.setStyleSheet(self._generate_btn_style(theme))

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.message_input.setPlaceholderText(t("git.commit_placeholder"))
        self.pull_btn.setToolTip(t("git.pull_tooltip"))
        self.push_btn.setToolTip(t("git.push_tooltip"))
        self.generate_btn.setText(t("git.generate_msg"))
        self.generate_btn.setToolTip(t("git.generate_msg_tooltip"))


class GitDiffDialog(QDialog):
    """Diff 查看对话框"""

    def __init__(self, title: str, diff_content: str, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self.setWindowTitle(title)
        self.setMinimumSize(600, 400)

        self._setup_ui(diff_content)

    def _setup_ui(self, diff_content: str):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Diff 显示区域
        self.diff_text = QTextEdit()
        self.diff_text.setReadOnly(True)
        self.diff_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.diff_text.setFont(_mono_font(12))

        # 设置 diff 内容（带语法高亮）
        self._set_diff_content(diff_content)

        self.diff_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
            }}
        """)
        layout.addWidget(self.diff_text)

        # 关闭按钮
        close_btn = QPushButton(t("git.close"))
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                margin: 8px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('bg_hover', '#4d4d6c')};
            }}
        """)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        self.setStyleSheet(f"""
            QDialog {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
        """)

    def _set_diff_content(self, diff_content: str):
        """设置 diff 内容（带简单的颜色高亮）"""
        html_lines = []

        for line in diff_content.splitlines():
            escaped_line = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            if line.startswith('+') and not line.startswith('+++'):
                color = '#98c379'  # 绿色 - 新增行
            elif line.startswith('-') and not line.startswith('---'):
                color = '#e06c75'  # 红色 - 删除行
            elif line.startswith('@@'):
                color = '#61afef'  # 蓝色 - 位置信息
            elif line.startswith('diff ') or line.startswith('index '):
                color = '#c678dd'  # 紫色 - 头部信息
            else:
                color = self.theme.get('text', '#eaeaea')

            html_lines.append(f'<span style="color: {color};">{escaped_line}</span>')

        self.diff_text.setHtml('<pre style="margin: 8px;">' + '<br>'.join(html_lines) + '</pre>')


class GitDiffView(QWidget):
    """左右并排的 diff 视图：左栏=旧/删除行，右栏=新/增加行，竖直滚动联动。

    内嵌在 Git 面板里（不弹窗）。把 `git diff` 的统一格式解析成两栏对齐显示。
    """
    closed = pyqtSignal()

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._syncing = False
        # hunk 级暂存/取消暂存的上下文（set_context 设置；不设置时纯展示）
        self._gm = None             # GitManager 引用
        self._ctx_path = None       # 当前 diff 对应的文件路径
        self._ctx_staged = False    # True=展示的是暂存区 diff
        # 每次 set_diff 重算：文件头行、各 hunk 原始文本、hunk 行映射等
        self._file_header = None    # ['diff --git ...', 'index ...', '--- ...', '+++ ...']
        self._hunk_patches = []     # 每个 hunk 的原始文本（含 @@ 行）
        self._hunk_row_map = {}     # 显示行号(block) -> hunk 下标
        self._marker_offsets = {}   # 显示行号(block) -> 行内可点击标记起始列
        self._marker_label = None   # 标记文案（None=本 diff 不支持 hunk 操作）
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部：返回 + 文件名
        self._header = QFrame()
        h = QHBoxLayout(self._header)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(8)
        self.back_btn = QPushButton(t("git.diff_back"))
        self.back_btn.clicked.connect(self.closed.emit)
        self.title_label = QLabel("")
        h.addWidget(self.back_btn)
        h.addWidget(self.title_label, 1)
        layout.addWidget(self._header)

        # 两栏并排
        self._split = QSplitter(Qt.Orientation.Horizontal)
        self.left_edit = self._make_edit()
        self.right_edit = self._make_edit()
        # 统一固定行高（代码字体行距 + 余量，容纳 CJK 回退字体），两栏每行严格等高
        self._fixed_line_height = QFontMetrics(self.left_edit.font()).height() + 4
        self._split.addWidget(self.left_edit)
        self._split.addWidget(self.right_edit)
        self._split.setSizes([500, 500])
        layout.addWidget(self._split, 1)

        # 竖直滚动联动
        self.left_edit.verticalScrollBar().valueChanged.connect(self._sync_from_left)
        self.right_edit.verticalScrollBar().valueChanged.connect(self._sync_from_right)

        self.apply_theme(self.theme)

    def _make_edit(self) -> QPlainTextEdit:
        # 用 QPlainTextEdit：竖直滚动以「行」为单位，两栏行数相同时按值同步 = 逐行精确对齐，
        # 不会像 QTextEdit 那样因像素高度细微差异（如横向滚动条）导致错位。
        e = QPlainTextEdit()
        e.setReadOnly(True)
        e.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        e.setFont(_mono_font(12))
        # hunk 头行内的「暂存/取消暂存此块」标记：在 viewport 上监听点击与悬停
        e.viewport().setMouseTracking(True)
        e.viewport().installEventFilter(self)
        return e

    def _sync_from_left(self, value: int):
        if self._syncing:
            return
        self._syncing = True
        self.right_edit.verticalScrollBar().setValue(value)
        self._syncing = False

    def _sync_from_right(self, value: int):
        if self._syncing:
            return
        self._syncing = True
        self.left_edit.verticalScrollBar().setValue(value)
        self._syncing = False

    def set_context(self, git_manager, file_path: str, staged: bool):
        """补充 hunk 级暂存/取消暂存所需的上下文。

        在 set_diff 之前调用。不调用（或 git_manager/file_path 为空）时，
        视图行为与纯展示完全一致，不出现任何可点击标记。
        """
        self._gm = git_manager
        self._ctx_path = file_path or None
        self._ctx_staged = bool(staged)

    def set_diff(self, title: str, diff_content: str):
        self.title_label.setText(title)
        # 重算 hunk 级操作所需数据；不满足条件（无上下文/新增/删除/二进制
        # 文件等）时 _marker_label 保持 None，hunk 头不出现可点击标记。
        self._file_header, self._hunk_patches = None, []
        self._hunk_row_map = {}
        self._marker_offsets = {}
        self._marker_label = None
        if self._gm is not None and self._ctx_path:
            header, hunks = self._parse_hunks(diff_content or "")
            if header and hunks:
                self._file_header = header
                self._hunk_patches = hunks
                self._marker_label = (
                    t("git.unstage_hunk") if self._ctx_staged else t("git.stage_hunk")
                )
        left_rows, right_rows, hunk_rows = self._parse(diff_content or "")
        if not left_rows and not right_rows:
            left_rows = [(None, t("git.diff_no_content"), 'ctx')]
            right_rows = [(None, '', 'ctx')]
        if self._marker_label:
            # 显示中的 hunk 行按出现顺序与 patch hunk 一一对应
            self._hunk_row_map = {
                row: i for i, row in enumerate(hunk_rows)
                if i < len(self._hunk_patches)
            }
        self._fill_edit(self.left_edit, left_rows)
        self._fill_edit(self.right_edit, right_rows)
        self.left_edit.verticalScrollBar().setValue(0)
        self.right_edit.verticalScrollBar().setValue(0)

    def _parse_hunks(self, diff_content: str):
        """把单文件 diff 拆成 (文件头行列表, [每个 hunk 的原始文本])。

        不支持 hunk 级操作的情形返回 (None, [])：多文件 diff、二进制 diff、
        新增/删除文件（整个文件本来就是一个整体，没有部分暂存的意义）、
        缺少 ---/+++ 头（无法构造合法 patch）。
        """
        header, hunks, cur = [], [], None
        seen_diff = 0
        for line in diff_content.splitlines():
            if line.startswith('diff --git'):
                seen_diff += 1
                if seen_diff > 1:
                    return None, []  # 多文件 diff 不支持
            if (line.startswith('Binary files') or line.startswith('GIT binary patch')
                    or line.startswith('new file mode')
                    or line.startswith('deleted file mode')):
                return None, []
            if line.startswith('@@'):
                cur = [line]
                hunks.append(cur)
            elif cur is not None:
                cur.append(line)  # hunk 体（含 "\ No newline ..."）原样保留
            else:
                header.append(line)
        if not hunks:
            return None, []
        if (not any(l.startswith('--- ') for l in header)
                or not any(l.startswith('+++ ') for l in header)):
            return None, []
        return header, ['\n'.join(h) for h in hunks]

    def _apply_hunk(self, hunk_index: int):
        """点击 hunk 头标记：暂存（未暂存视图）或取消暂存（暂存视图）该 hunk。

        成功后用最新 diff 刷新自身；文件列表由 GitManager.status_changed
        信号驱动 GitPanel 刷新。失败走 GitManager.error_occurred 现有弹窗。
        """
        if self._gm is None or not self._ctx_path or not self._file_header:
            return
        if not (0 <= hunk_index < len(self._hunk_patches)):
            return
        patch = '\n'.join(self._file_header + [self._hunk_patches[hunk_index]]) + '\n'
        # 暂存：patch 来自 index→worktree 的 diff，正向应用到 index；
        # 取消暂存：patch 来自 HEAD→index 的 diff，反向应用到 index。
        if self._gm.apply_patch(patch, cached=True, reverse=self._ctx_staged):
            new_diff = self._gm.get_diff(self._ctx_path, self._ctx_staged)
            self.set_diff(self.title_label.text(), new_diff)

    def _hunk_hit(self, edit, pos):
        """命中测试：pos 是否落在某个 hunk 头行的可点击标记上。"""
        if not self._marker_label or not self._hunk_row_map:
            return None
        cursor = edit.cursorForPosition(pos)
        row = cursor.blockNumber()
        hunk_idx = self._hunk_row_map.get(row)
        if hunk_idx is None:
            return None
        rect = edit.cursorRect(cursor)
        if pos.y() < rect.top() or pos.y() > rect.bottom():
            return None  # 点在文档下方空白处时 cursorForPosition 会钳到最后一行
        offset = self._marker_offsets.get(row)
        if offset is None or cursor.positionInBlock() < offset:
            return None
        return hunk_idx

    def eventFilter(self, obj, event):
        if obj is self.left_edit.viewport() or obj is self.right_edit.viewport():
            edit = self.left_edit if obj is self.left_edit.viewport() else self.right_edit
            etype = event.type()
            if (etype == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton):
                idx = self._hunk_hit(edit, event.position().toPoint())
                if idx is not None:
                    self._apply_hunk(idx)
                    return True
            elif etype == QEvent.Type.MouseMove:
                idx = self._hunk_hit(edit, event.position().toPoint())
                obj.setCursor(
                    Qt.CursorShape.PointingHandCursor if idx is not None
                    else Qt.CursorShape.IBeamCursor
                )
        return super().eventFilter(obj, event)

    def _line_bg_brush(self, kind: str):
        """整行背景：删除=半透明红、新增=半透明绿、对齐占位=半透明斜条纹、
        hunk 头=淡蓝；上下文无背景。"""
        if kind == 'del':
            return QBrush(QColor(229, 83, 75, 60))      # 红 ~0.23
        if kind == 'add':
            return QBrush(QColor(63, 185, 80, 60))       # 绿 ~0.23
        if kind == 'hunk':
            return QBrush(QColor(97, 175, 239, 38))      # 淡蓝
        if kind == 'pad':
            # 斜条纹半透明背景，标记"另一边有增/删"，方便对齐
            return QBrush(QColor(140, 140, 140, 70), Qt.BrushStyle.BDiagPattern)
        return None  # ctx：透明

    def _fill_edit(self, edit, rows):
        """用 QTextBlockFormat 逐行写入：整行背景填满（不是只染文字），
        行号灰色，代码用主题前景色。

        关键：给每一行设固定行高。否则含中文/emoji 的行会因回退字体更高而变高，
        两栏逐行累积错位。固定行高让两栏每行严格等高、精确对齐。
        """
        edit.clear()
        num_color = QColor('#6a6a7a')
        text_color = QColor(self.theme.get('text', '#eaeaea'))
        # 行高取代码字体行距 + 余量，足以容纳 CJK 回退字体，避免裁切
        line_h = self._fixed_line_height
        cursor = QTextCursor(edit.document())
        cursor.beginEditBlock()
        first = True
        for i, (ln, text, kind) in enumerate(rows):
            if not first:
                cursor.insertBlock()
            first = False
            block_fmt = QTextBlockFormat()
            block_fmt.setLineHeight(
                line_h, QTextBlockFormat.LineHeightTypes.FixedHeight.value
            )
            brush = self._line_bg_brush(kind)
            if brush is not None:
                block_fmt.setBackground(brush)
            cursor.setBlockFormat(block_fmt)
            num_fmt = QTextCharFormat()
            num_fmt.setForeground(num_color)
            cursor.insertText((f'{ln:>5} ' if ln else '      '), num_fmt)
            txt_fmt = QTextCharFormat()
            txt_fmt.setForeground(text_color)
            cursor.insertText(text or '', txt_fmt)
            # hunk 头行尾追加可点击标记「暂存此块 / 取消暂存此块」
            # （左右两栏 hunk 行文本相同 → 标记起始列一致，offset 共用）
            if kind == 'hunk' and self._marker_label and i in self._hunk_row_map:
                self._marker_offsets[i] = 6 + len(text or '') + 2
                mark_fmt = QTextCharFormat()
                mark_fmt.setForeground(QColor('#61afef'))
                mark_fmt.setFontUnderline(True)
                cursor.insertText('  ' + self._marker_label, mark_fmt)
        cursor.endEditBlock()

    def _parse(self, diff_content: str):
        """把统一 diff 解析成左右两列对齐的行列表。

        每行是 (lineno, text, kind)，kind ∈ {ctx, del, add, hunk, pad}。
        删除/新增成对时左右对齐，数量不等时短的一侧补 pad 空行。
        另返回 hunk 头所在的显示行号列表（与 _parse_hunks 的 hunk 顺序对应）。
        """
        import re
        left, right = [], []
        hunk_rows = []
        old_ln = new_ln = 0
        pend_del, pend_add = [], []
        MAX_ROWS = 6000

        def flush():
            n = max(len(pend_del), len(pend_add))
            for k in range(n):
                left.append(pend_del[k] if k < len(pend_del) else (None, '', 'pad'))
                right.append(pend_add[k] if k < len(pend_add) else (None, '', 'pad'))
            pend_del.clear()
            pend_add.clear()

        skip_prefixes = ('diff ', 'index ', '--- ', '+++ ', 'new file', 'deleted file',
                         'old mode', 'new mode', 'similarity ', 'rename ', '\\ No newline')
        for line in diff_content.splitlines():
            if line.startswith(skip_prefixes):
                continue
            if line.startswith('@@'):
                flush()
                m = re.search(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if m:
                    old_ln, new_ln = int(m.group(1)), int(m.group(2))
                left.append((None, line, 'hunk'))
                right.append((None, line, 'hunk'))
                hunk_rows.append(len(left) - 1)
                continue
            if line.startswith('-'):
                pend_del.append((old_ln, line[1:], 'del'))
                old_ln += 1
            elif line.startswith('+'):
                pend_add.append((new_ln, line[1:], 'add'))
                new_ln += 1
            else:
                flush()
                text = line[1:] if line.startswith(' ') else line
                left.append((old_ln, text, 'ctx'))
                right.append((new_ln, text, 'ctx'))
                old_ln += 1
                new_ln += 1
            if len(left) > MAX_ROWS:
                break
        flush()
        return left, right, hunk_rows

    def apply_theme(self, theme: dict):
        self.theme = theme
        edit_css = f"""
            QPlainTextEdit {{
                background-color: {theme.get('bg_dark', '#1a1a2e')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
            }}
        """
        self.left_edit.setStyleSheet(edit_css)
        self.right_edit.setStyleSheet(edit_css)
        self._header.setStyleSheet(f"""
            QFrame {{
                background-color: {theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {theme.get('border', '#3d3d5c')};
            }}
        """)
        self.title_label.setStyleSheet(
            f"color: {theme.get('text', '#eaeaea')}; font-weight: bold;"
        )
        self.back_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 4px 12px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """)

    def apply_language(self):
        self.back_btn.setText(t("git.diff_back"))


class GitOutputView(QWidget):
    """单栏只读输出视图：展示 push/pull 的完整 git 输出（进度、fast-forward、文件统计）。

    内嵌在主内容区（不弹窗），带返回按钮回到终端。
    """
    closed = pyqtSignal()

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QFrame()
        h = QHBoxLayout(self._header)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(8)
        self.back_btn = QPushButton(t("git.diff_back"))
        self.back_btn.clicked.connect(self.closed.emit)
        self.title_label = QLabel("")
        h.addWidget(self.back_btn)
        h.addWidget(self.title_label, 1)
        layout.addWidget(self._header)

        self.output_edit = QTextEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.output_edit.setFont(_mono_font(12))
        layout.addWidget(self.output_edit, 1)

        self.apply_theme(self.theme)

    def set_output(self, title: str, text: str):
        self.title_label.setText(title)
        # 轻量着色：diff 增删行红绿、远程/合并信息蓝绿、其余普通
        fg = self.theme.get('text', '#eaeaea')
        lines = []
        for line in (text or '').splitlines():
            etext = line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            c = fg
            stripped = line.strip()
            if line.startswith('+') and not line.startswith('+++'):
                c = '#98c379'   # 新增行 绿
            elif line.startswith('-') and not line.startswith('---'):
                c = '#e06c75'   # 删除行 红
            elif line.startswith('@@'):
                c = '#61afef'
            elif line.startswith('commit ') or line.startswith('Author:') or line.startswith('Date:'):
                c = '#e5c07b'   # 提交头 黄
            elif stripped.startswith('remote:') or stripped.startswith('来自') or '->' in stripped:
                c = '#61afef'
            elif 'Fast-forward' in line or 'Updating' in line or '更新' in line:
                c = '#98c379'
            lines.append(f'<span style="color:{c};">{etext or "&nbsp;"}</span>')
        self.output_edit.setHtml('<pre style="margin:0; padding:6px;">' + '\n'.join(lines) + '</pre>')
        self.output_edit.verticalScrollBar().setValue(0)

    def apply_theme(self, theme: dict):
        self.theme = theme
        self.output_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: {theme.get('bg_dark', '#1a1a2e')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
            }}
        """)
        self._header.setStyleSheet(f"""
            QFrame {{
                background-color: {theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {theme.get('border', '#3d3d5c')};
            }}
        """)
        self.title_label.setStyleSheet(
            f"color: {theme.get('text', '#eaeaea')}; font-weight: bold;"
        )
        self.back_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                color: {theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                padding: 4px 12px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """)

    def apply_language(self):
        self.back_btn.setText(t("git.diff_back"))


class _GraphCanvas(QWidget):
    """提交历史 graph 画布：左侧 lane 点线（仿 VS Code），右侧 引用徽标 + 标题 + 作者。"""

    commit_clicked = pyqtSignal(str)            # 提交 hash
    revert_requested = pyqtSignal(str)          # 撤销提交（git revert）
    reset_requested = pyqtSignal(str, str)      # 重置到提交 (hash, mode)
    copy_hash_requested = pyqtSignal(str)       # 复制提交 hash

    LANE_COLORS = ['#e06c75', '#61afef', '#98c379', '#c678dd',
                   '#e5c07b', '#56b6c2', '#d19a66', '#abb2bf']

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._rows = []          # 每行 lane 布局
        self._max_cols = 1
        self._row_h = 24
        self._lane_w = 14
        self._dot_r = 4
        self._left_pad = 8
        self._hover = -1
        # 用像素字号，和导航/列表的 QSS `font-size: Npx` 视觉一致（point 会偏大偏"肥"）
        self._font = _mono_font(12, pixel=True)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_commits(self, commits: list):
        self._rows = self._compute_lanes(commits)
        self._max_cols = max((r['width'] for r in self._rows), default=1)
        self.setMinimumHeight(len(self._rows) * self._row_h)
        self.updateGeometry()
        self.update()

    def set_font_size(self, size: int):
        """按 GUI 字号设置 graph 字体，并同比例缩放行高/lane 几何，避免大字号被裁。

        以 12pt 为基准（size=12 复现 __init__ 的默认 lane_w=14 / dot_r=4 /
        left_pad=8 / row_h=24），所以默认态不跳变。
        """
        size = max(6, min(32, int(size)))
        self._font = _mono_font(size, pixel=True)
        scale = size / 12.0
        self._lane_w = max(10, round(14 * scale))
        self._dot_r = max(3, round(4 * scale))
        self._left_pad = max(6, round(8 * scale))
        self._row_h = max(18, QFontMetrics(self._font).height() + round(8 * scale))
        if self._rows:
            self.setMinimumHeight(len(self._rows) * self._row_h)
        self.updateGeometry()
        self.update()

    @staticmethod
    def _compute_lanes(commits: list) -> list:
        """把提交序列排成 lane：返回每行 {commit, above, my, below, parents, width}。
        above/below 是该行上/下边的活动 lane（hash 列表），my 是该提交的 dot 所在列。"""
        rows = []
        above = []  # 进入当前行的 lane（= 上一行的 below）
        for c in commits:
            h, parents = c['hash'], c['parents']
            my = above.index(h) if h in above else len(above)
            below_src = list(above)
            while len(below_src) <= my:
                below_src.append(None)
            if parents:
                below_src[my] = parents[0]
                for p in parents[1:]:
                    if p not in below_src:
                        for j in range(len(below_src)):
                            if below_src[j] is None:
                                below_src[j] = p
                                break
                        else:
                            below_src.append(p)
            else:
                below_src[my] = None
            # 压缩：去掉 None 和重复（lane 合并）
            below, seen = [], set()
            for hh in below_src:
                if hh is None or hh in seen:
                    continue
                seen.add(hh)
                below.append(hh)
            width = max(len(above), len(below), my + 1)
            rows.append({'commit': c, 'above': list(above), 'my': my,
                         'below': below, 'parents': parents, 'width': width})
            above = below
        return rows

    def _lane_color(self, col: int) -> str:
        return self.LANE_COLORS[col % len(self.LANE_COLORS)]

    def _row_at(self, y: int) -> int:
        idx = y // self._row_h
        return idx if 0 <= idx < len(self._rows) else -1

    def mouseMoveEvent(self, event):
        idx = self._row_at(int(event.position().y()))
        if idx != self._hover:
            self._hover = idx
            self.update()

    def leaveEvent(self, event):
        if self._hover != -1:
            self._hover = -1
            self.update()

    def mousePressEvent(self, event):
        idx = self._row_at(int(event.position().y()))
        if idx >= 0:
            self.commit_clicked.emit(self._rows[idx]['commit']['hash'])

    def contextMenuEvent(self, event):
        """右键提交行：撤销(revert) / 重置(reset) / 复制哈希。"""
        from PyQt6.QtWidgets import QMenu
        idx = self._row_at(int(event.pos().y()))
        if idx < 0:
            return
        commit = self._rows[idx]['commit']
        h = commit['hash']
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background-color: #252526; color: #cccccc;"
            " border: 1px solid #454545; }"
            "QMenu::item { padding: 6px 24px 6px 12px; }"
            "QMenu::item:selected { background-color: #094771; }"
            "QMenu::separator { height: 1px; background: #454545; margin: 4px 8px; }"
        )
        revert_act = menu.addAction(t("git.ctx_revert"))
        menu.addSeparator()
        reset_soft_act = menu.addAction(t("git.ctx_reset_soft"))
        reset_mixed_act = menu.addAction(t("git.ctx_reset_mixed"))
        reset_hard_act = menu.addAction(t("git.ctx_reset_hard"))
        menu.addSeparator()
        copy_act = menu.addAction(t("git.ctx_copy_hash"))
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen == revert_act:
            self.revert_requested.emit(h)
        elif chosen == reset_soft_act:
            self.reset_requested.emit(h, 'soft')
        elif chosen == reset_mixed_act:
            self.reset_requested.emit(h, 'mixed')
        elif chosen == reset_hard_act:
            self.reset_requested.emit(h, 'hard')
        elif chosen == copy_act:
            self.copy_hash_requested.emit(h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self._font)
        fm = QFontMetrics(self._font)
        w = self.width()
        row_h, lane_w, r = self._row_h, self._lane_w, self._dot_r

        def col_x(c):
            return self._left_pad + c * lane_w + lane_w // 2

        rect = event.rect()
        first = max(0, rect.top() // row_h - 1)
        last = min(len(self._rows) - 1, rect.bottom() // row_h + 1)
        text_color = QColor(self.theme.get('text', '#eaeaea'))
        dim_color = QColor(self.theme.get('text_dim', '#888'))

        for i in range(first, last + 1):
            row = self._rows[i]
            y_top = i * row_h
            y_mid = y_top + row_h // 2
            y_bot = y_top + row_h
            above, below, my = row['above'], row['below'], row['my']
            h = row['commit']['hash']

            if i == self._hover:
                painter.fillRect(0, y_top, w, row_h,
                                 QColor(self.theme.get('bg_hover', '#2d2d44')))

            # 上半：来自上方的 lane（合并进 dot 或穿过到下方）
            for a_idx, ah in enumerate(above):
                painter.setPen(QPen(QColor(self._lane_color(a_idx)), 2))
                if ah == h:
                    painter.drawLine(col_x(a_idx), y_top, col_x(my), y_mid)
                else:
                    bcol = below.index(ah) if ah in below else a_idx
                    painter.drawLine(col_x(a_idx), y_top, col_x(bcol), y_bot)

            # 下半：dot 连向各父提交
            for p in row['parents']:
                if p in below:
                    bcol = below.index(p)
                    painter.setPen(QPen(QColor(self._lane_color(bcol)), 2))
                    painter.drawLine(col_x(my), y_mid, col_x(bcol), y_bot)

            # dot
            dot = QColor(self._lane_color(my))
            painter.setBrush(QBrush(dot))
            painter.setPen(QPen(dot, 1))
            painter.drawEllipse(QPoint(col_x(my), y_mid), r, r)

            # 文本起点。作者列先定位：徽标与标题都不得越过 author_x，
            # 否则窄面板 + 多引用（main/tag/origin/…）时右对齐的作者名
            # 会直接压在徽章/文字上
            tx = self._left_pad + self._max_cols * lane_w + 8
            author = row['commit']['author']
            aw = fm.horizontalAdvance(author) + 12
            author_x = w - aw - 8

            # 引用徽标（branch / HEAD / tag）；放不下的用 "…" 指示
            refs = row['commit']['refs']
            for i, ref in enumerate(refs):
                label = ref.replace('HEAD -> ', '')
                is_head = ref.startswith('HEAD')
                is_remote = label.startswith('origin/') or '/' in label
                badge_bg = QColor(self.theme.get('accent', '#667eea')) if is_head \
                    else (QColor('#4b5263') if is_remote else QColor('#3a7a3a'))
                bw = fm.horizontalAdvance(label) + 12
                if tx + bw > author_x - 6:
                    ell = '…'
                    ew = fm.horizontalAdvance(ell) + 4
                    if tx + ew <= author_x - 6:
                        painter.setPen(dim_color)
                        painter.drawText(QRect(tx, y_top, ew, row_h),
                                         Qt.AlignmentFlag.AlignVCenter, ell)
                        tx += ew + 4
                    break
                painter.setBrush(QBrush(badge_bg))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(QRect(tx, y_mid - fm.height() // 2 - 1,
                                              bw, fm.height() + 2), 4, 4)
                painter.setPen(QColor('#ffffff'))
                painter.drawText(QRect(tx, y_top, bw, row_h),
                                 Qt.AlignmentFlag.AlignCenter, label)
                tx += bw + 4

            # 标题（放不下就不画，绝不与作者区重叠）+ 作者（右对齐、暗色）
            subj_w = author_x - tx - 4
            if subj_w > 20:
                painter.setPen(text_color)
                subj = fm.elidedText(row['commit']['subject'],
                                     Qt.TextElideMode.ElideRight, subj_w)
                painter.drawText(QRect(tx, y_top, subj_w, row_h),
                                 Qt.AlignmentFlag.AlignVCenter, subj)
            painter.setPen(dim_color)
            painter.drawText(QRect(author_x, y_top, aw, row_h),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             author)
        painter.end()


class GitGraphWidget(QWidget):
    """提交历史 graph 面板：顶部 "GRAPH" 标题 + 可滚动的 graph 画布。"""

    commit_clicked = pyqtSignal(str)
    revert_requested = pyqtSignal(str)
    reset_requested = pyqtSignal(str, str)
    copy_hash_requested = pyqtSignal(str)
    load_more_requested = pyqtSignal(int)   # 已加载条数（= 下一页的 skip）

    LOAD_MORE_MARGIN = 240  # 滚动到距底部这么多像素内就预取下一页

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._commits = []       # 已加载的全部提交（含翻页追加的）
        self._has_more = False   # 仓库里是否还有更旧的提交没加载
        self._loading = False    # 下一页请求是否在途（防重复触发）
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._title = QLabel(t("git.graph_title"))
        layout.addWidget(self._title)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._canvas = _GraphCanvas(self.theme)
        self._canvas.commit_clicked.connect(self.commit_clicked.emit)
        self._canvas.revert_requested.connect(self.revert_requested.emit)
        self._canvas.reset_requested.connect(self.reset_requested.emit)
        self._canvas.copy_hash_requested.connect(self.copy_hash_requested.emit)
        self._scroll.setWidget(self._canvas)
        self._scroll.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        layout.addWidget(self._scroll, 1)

        self.apply_theme(self.theme)

    def set_commits(self, commits: list, has_more: bool = False):
        """全量刷新：替换整个列表（翻页在途的旧请求会因 skip 不匹配被丢弃）。"""
        self._commits = list(commits)
        self._has_more = has_more
        self._loading = False
        self._canvas.set_commits(self._commits)

    def commit_count(self) -> int:
        return len(self._commits)

    def append_commits(self, commits: list, skip: int, has_more: bool):
        """追加下一页提交并重算 graph 连线。

        skip 与当前已加载条数不符说明期间发生过全量刷新，这页数据基于旧
        偏移量取的，直接丢弃（用户再滚到底会按新偏移重新取）。
        """
        self._loading = False
        if skip != len(self._commits):
            return
        self._has_more = has_more and bool(commits)
        if not commits:
            return
        # 两次取页之间如果有新提交进来，--skip 偏移会错位造成重复，按 hash 去重
        seen = {c['hash'] for c in self._commits}
        fresh = [c for c in commits if c['hash'] not in seen]
        if not fresh:
            return
        self._commits.extend(fresh)
        self._canvas.set_commits(self._commits)

    def _maybe_load_more(self, *_):
        if not self._has_more or self._loading:
            return
        sb = self._scroll.verticalScrollBar()
        if sb.maximum() <= 0:
            return
        if sb.value() >= sb.maximum() - self.LOAD_MORE_MARGIN:
            self._loading = True
            self.load_more_requested.emit(len(self._commits))

    def set_font_size(self, size: int):
        self._canvas.set_font_size(size)

    def apply_theme(self, theme: dict):
        self.theme = theme
        self._canvas.theme = theme
        self._title.setStyleSheet(f"""
            QLabel {{
                color: {theme.get('text_dim', '#888')};
                background-color: {theme.get('bg_medium', '#16213e')};
                font-size: 11px; font-weight: bold;
                padding: 4px 10px;
                border-bottom: 1px solid {theme.get('border', '#3d3d5c')};
            }}
        """)
        self._scroll.setStyleSheet(f"background-color: {theme.get('bg_dark', '#1a1a2e')}; border: none;")
        self._canvas.setStyleSheet(f"background-color: {theme.get('bg_dark', '#1a1a2e')};")
        self._canvas.update()

    def apply_language(self):
        self._title.setText(t("git.graph_title"))


class GitHeaderWidget(QFrame):
    """Git 面板头部"""

    # 信号
    branch_changed = pyqtSignal(str)              # 兼容保留：仅本地分支切换时触发
    ref_changed = pyqtSignal(str, str)            # (kind, name)；kind ∈ {'local','remote','tag'}
    refresh_clicked = pyqtSignal()
    stash_clicked = pyqtSignal()
    settings_clicked = pyqtSignal()
    delete_branch_requested = pyqtSignal(str)     # 用户右键菜单确认删除本地分支

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)

        # 标题
        self.title_label = QLabel(t("git.source_control"))
        self.title_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text', '#eaeaea')};
                font-weight: bold;
                font-size: 13px;
            }}
        """)
        layout.addWidget(self.title_label)

        layout.addStretch()

        # 分支选择器
        self.branch_combo = QComboBox()
        self.branch_combo.setMinimumWidth(120)
        self.branch_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QComboBox:hover {{
                border-color: {self.theme.get('accent', '#667eea')};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                selection-background-color: {self.theme.get('accent', '#667eea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)
        self.branch_combo.currentIndexChanged.connect(self._on_combo_changed)
        # 右键菜单：在下拉列表里右击本地分支可删除（远程/tag/当前分支不显示删除项）
        view = self.branch_combo.view()
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._on_branch_combo_context_menu)
        layout.addWidget(self.branch_combo)

        # 刷新 / 设置：两个统一风格的线条图标按钮（图标在 _update_style 里按主题色绘制）
        # 「新建分支」已移入设置菜单，避免它紧挨刷新按钮时被误点而意外建分支。
        self.refresh_btn = QPushButton()
        self.refresh_btn.setToolTip(t("git.refresh_tooltip"))
        self.refresh_btn.setFixedSize(28, 28)
        self.refresh_btn.setIconSize(QSize(16, 16))
        self.refresh_btn.clicked.connect(self.refresh_clicked.emit)
        layout.addWidget(self.refresh_btn)

        # Stash：贮藏当前修改 / 管理已有 stash
        self.stash_btn = QPushButton()
        self.stash_btn.setToolTip(t("git.stash_tooltip"))
        self.stash_btn.setFixedSize(28, 28)
        self.stash_btn.setIconSize(QSize(16, 16))
        self.stash_btn.clicked.connect(self.stash_clicked.emit)
        layout.addWidget(self.stash_btn)

        self.settings_btn = QPushButton()
        self.settings_btn.setToolTip(t("git.settings_tooltip"))
        self.settings_btn.setFixedSize(28, 28)
        self.settings_btn.setIconSize(QSize(16, 16))
        self.settings_btn.clicked.connect(self.settings_clicked.emit)
        layout.addWidget(self.settings_btn)

        self._update_style()

    def _update_style(self):
        """更新样式"""
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)

    def update_branches(self, branches: list, head_ref, tags: list = None):
        """更新分支/Tag 列表

        Args:
            branches: GitBranch 列表（本地 + 远程）
            head_ref: 当前 HEAD 引用 (kind, name)；亦可传入 str 表示本地分支名（兼容旧调用）
            tags: tag 名列表
        """
        tags = tags or []
        if isinstance(head_ref, str):
            head_ref = ('local', head_ref)
        head_kind, head_name = head_ref

        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()

        locals_ = [b for b in branches if not b.is_remote]
        remotes = [b for b in branches if b.is_remote]

        # 过滤掉"和本地同名"的远程项：点了等于切回本地，没有独立价值
        local_names = {b.name for b in locals_}
        remotes = [
            b for b in remotes
            if (b.name.split('/', 1)[1] if '/' in b.name else b.name) not in local_names
        ]

        # detached 且不在 tag 上：插入一个占位项指示当前状态
        if head_kind == 'detached':
            self.branch_combo.addItem(f"(detached {head_name})", ('detached', head_name))

        # 本地分支
        for b in locals_:
            self.branch_combo.addItem(b.name, ('local', b.name))

        # 远程独有分支
        if remotes:
            if self.branch_combo.count() > 0:
                self.branch_combo.insertSeparator(self.branch_combo.count())
            for b in remotes:
                self.branch_combo.addItem(b.name, ('remote', b.name))

        # Tags
        if tags:
            if self.branch_combo.count() > 0:
                self.branch_combo.insertSeparator(self.branch_combo.count())
            for tag in tags:
                self.branch_combo.addItem(f"tag: {tag}", ('tag', tag))

        # 按 HEAD 引用类型精确选中对应项
        for i in range(self.branch_combo.count()):
            data = self.branch_combo.itemData(i)
            if data and data[0] == head_kind and data[1] == head_name:
                self.branch_combo.setCurrentIndex(i)
                break

        self.branch_combo.blockSignals(False)

    def _on_combo_changed(self, index: int):
        """combo 选择变更 → 派发 ref_changed / branch_changed"""
        if index < 0:
            return
        data = self.branch_combo.itemData(index)
        if not data:
            return
        kind, name = data
        self.ref_changed.emit(kind, name)
        if kind == 'local':
            self.branch_changed.emit(name)

    def _on_branch_combo_context_menu(self, pos):
        """下拉列表右键菜单：仅本地分支可删除（当前分支自身除外）。

        - 远程分支：删除是 push --delete，破坏性强，留给终端处理
        - tag：留给终端处理
        - 当前 HEAD 指向的本地分支：git 拒绝删除，直接不显示菜单项
        """
        view = self.branch_combo.view()
        index = view.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        data = self.branch_combo.itemData(row)
        if not data:
            return
        kind, name = data
        if kind != 'local':
            return
        # 当前分支自身不允许删除
        cur_data = self.branch_combo.currentData()
        if cur_data and cur_data[0] == 'local' and cur_data[1] == name:
            return

        menu = QMenu(view)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 24px;
            }}
            QMenu::item:selected {{
                background-color: {self.theme.get('accent', '#667eea')};
            }}
        """)
        act_del = QAction(t("git.delete_branch_menu", name=name), self)

        def _trigger():
            # 先收起下拉，避免删除确认对话框被它遮挡 / 抢焦点
            self.branch_combo.hidePopup()
            self.delete_branch_requested.emit(name)

        act_del.triggered.connect(_trigger)
        menu.addAction(act_del)
        menu.exec(view.viewport().mapToGlobal(pos))

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()

        self.title_label.setStyleSheet(f"""
            QLabel {{
                color: {theme.get('text', '#eaeaea')};
                font-weight: bold;
                font-size: 13px;
            }}
        """)

        self.branch_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {theme.get('bg_medium', '#16213e')};
                color: {theme.get('text', '#eaeaea')};
                border: 1px solid {theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 4px 8px;
            }}
            QComboBox:hover {{
                border-color: {theme.get('accent', '#667eea')};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {theme.get('bg_medium', '#16213e')};
                color: {theme.get('text', '#eaeaea')};
                selection-background-color: {theme.get('accent', '#667eea')};
                border: 1px solid {theme.get('border', '#3d3d5c')};
            }}
        """)

        btn_style = f"""
            QPushButton {{
                background-color: {theme.get('bg_lighter', '#3d3d5c')};
                border: none;
                border-radius: 4px;
                padding: 0px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('bg_hover', '#4d4d6c')};
            }}
        """
        self.refresh_btn.setStyleSheet(btn_style)
        self.stash_btn.setStyleSheet(btn_style)
        self.settings_btn.setStyleSheet(btn_style)

        # 用主题前景色重绘线条图标，保证大小/粗细/对齐一致
        icon_color = theme.get('text', '#eaeaea')
        self.refresh_btn.setIcon(_make_git_tool_icon('refresh', icon_color))
        self.stash_btn.setIcon(_make_git_tool_icon('stash', icon_color))
        self.settings_btn.setIcon(_make_git_tool_icon('gear', icon_color))

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.title_label.setText(t("git.source_control"))
        self.refresh_btn.setToolTip(t("git.refresh_tooltip"))
        self.stash_btn.setToolTip(t("git.stash_tooltip"))
        self.settings_btn.setToolTip(t("git.settings_tooltip"))


class GitPanel(QWidget):
    """Git 管理面板"""

    # 用户拖拽分隔条改变提交区高度时发出（主窗口据此持久化）
    commit_height_changed = pyqtSignal(int)
    # 用户拖拽分隔条改变 body 各栏高度时发出完整 sizes（主窗口据此持久化）
    body_sizes_changed = pyqtSignal(list)
    # 双击文件请求查看 diff，交给主窗口在右侧大空间显示
    # (title, diff_content, file_path, staged) —— 后两项供 hunk 级暂存用
    diff_requested = pyqtSignal(str, str, str, bool)
    # pull 等操作完成后，把 git 输出交给主窗口在右侧大空间显示 (title, output)
    output_requested = pyqtSignal(str, str)

    @property
    def git_manager(self):
        """暴露内部 GitManager（如 GitDiffView 的 hunk 级暂存需要）。"""
        return self._git_manager

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._desired_commit_height = 0  # 记忆的提交区高度（0=未设置，用默认）
        self._desired_body_sizes = None   # 记忆的 body splitter 各栏高度（None=未设置）
        self._git_manager = GitManager(self)
        self._last_fetch_ts = 0.0  # 上次后台 fetch 的时间（节流用）
        self._active_workers = set()  # 在跑的后台线程，关闭时统一等待，避免被销毁时 abort
        self._fetch_running = False
        self._commit_running = False
        self._checkout_running = False
        self._refresh_running = False   # 后台全量刷新是否进行中
        self._refresh_pending = False   # 进行中又被请求 → 跑完后补一次（合并）
        self._status_refresh_running = False  # 后台轻量状态刷新是否进行中
        self._log_page_running = False  # graph 翻页加载是否进行中
        self._status_stale = False      # 隐藏期间跳过了刷新 → 重新可见时补一次
        self._last_error_message = None  # 上次弹过的错误文案，去重连珠弹窗（如目录被删后每次轮询都报同一错）

        self._setup_ui()
        self._connect_signals()

        # 从配置文件恢复用户设置的 git 代理（如果有）
        self._apply_persisted_git_proxy()

        # 定时后台 fetch：刷新远程跟踪分支，让"可 pull 条数"保持最新（仅面板可见时）
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setInterval(180_000)  # 3 分钟
        self._fetch_timer.timeout.connect(self._tick_fetch)
        self._fetch_timer.start()

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 头部
        self.header = GitHeaderWidget(self.theme)
        layout.addWidget(self.header)

        # 合并状态提示条：仓库处于 merge 中 / 存在未解决冲突时显示在头部正下方。
        # 用固定的警示色（不随主题），确保任何主题下都足够醒目。
        self.merge_banner = QFrame()
        mb_layout = QHBoxLayout(self.merge_banner)
        mb_layout.setContentsMargins(10, 6, 10, 6)
        mb_layout.setSpacing(8)
        self.merge_label = QLabel(t("git.merge_in_progress"))
        self.merge_label.setWordWrap(True)
        mb_layout.addWidget(self.merge_label, 1)
        self.abort_merge_btn = QPushButton(t("git.merge_abort_btn"))
        self.abort_merge_btn.clicked.connect(self._on_abort_merge)
        mb_layout.addWidget(self.abort_merge_btn)
        self.merge_banner.setStyleSheet("""
            QFrame {
                background-color: #5c2b2e;
                border-bottom: 1px solid #8a3a3f;
            }
        """)
        self.merge_label.setStyleSheet(
            "color: #ffd7d7; font-weight: bold; font-size: 12px;"
            " background: transparent; border: none;"
        )
        self.abort_merge_btn.setStyleSheet("""
            QPushButton {
                background-color: #e06c75;
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #ef7680;
            }
        """)
        self.merge_banner.hide()
        layout.addWidget(self.merge_banner)

        # 变更列表 + 提交区放进一个竖直分隔器：拖拽中间的分隔条即可上下调整
        # 二者高度（把提交信息框拉大/拉小）。
        self.body_splitter = QSplitter(Qt.Orientation.Vertical)
        self.body_splitter.setChildrenCollapsible(False)
        self.body_splitter.setHandleWidth(6)

        self.changes_widget = GitChangesWidget(self.theme)
        self.changes_widget.setMinimumHeight(120)
        self.body_splitter.addWidget(self.changes_widget)

        self.commit_widget = GitCommitWidget(self.theme)
        self.commit_widget.setMinimumHeight(150)
        self.body_splitter.addWidget(self.commit_widget)

        # 提交历史 graph（仿 VS Code）放在最下方，可拖拽分隔条调整高度
        self.graph_widget = GitGraphWidget(self.theme)
        self.graph_widget.setMinimumHeight(140)
        self.graph_widget.commit_clicked.connect(self._on_commit_clicked)
        self.graph_widget.revert_requested.connect(self._on_revert_commit)
        self.graph_widget.reset_requested.connect(self._on_reset_commit)
        self.graph_widget.copy_hash_requested.connect(self._on_copy_commit_hash)
        self.graph_widget.load_more_requested.connect(self._on_load_more_commits)
        self.body_splitter.addWidget(self.graph_widget)

        # 变更列表 + graph 吃掉多余空间，提交区默认停在它的自然高度
        self.body_splitter.setStretchFactor(0, 1)
        self.body_splitter.setStretchFactor(1, 0)
        self.body_splitter.setStretchFactor(2, 2)
        self.body_splitter.setSizes([200, 180, 360])
        # 记忆用户拖拽过的提交区高度
        self.body_splitter.splitterMoved.connect(self._on_splitter_moved)

        # 把分隔器放进竖直滚动区：屏幕不够高时各栏会维持各自的最小高度，
        # 整体溢出则出现竖直滚动条，避免提交框 / graph 被挤到看不见。
        self.body_scroll = QScrollArea()
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.body_scroll.setWidget(self.body_splitter)
        layout.addWidget(self.body_scroll, 1)

        # 无仓库提示
        self.no_repo_label = QLabel(t("git.no_repo"))
        self.no_repo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_repo_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text_dim', '#888')};
                font-size: 13px;
                padding: 20px;
            }}
        """)
        self.no_repo_label.hide()
        layout.addWidget(self.no_repo_label)

        self._update_style()

    def _update_style(self):
        """更新样式"""
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
            QSplitter::handle:vertical {{
                background-color: {self.theme.get('border', '#3d3d5c')};
                margin: 0 8px;
                border-radius: 2px;
            }}
            QSplitter::handle:vertical:hover {{
                background-color: {self.theme.get('accent', '#667eea')};
            }}
        """)

    def _on_splitter_moved(self, *_):
        """用户拖动分隔条 → 记住 body 各栏高度并通知外部持久化。"""
        sizes = self.body_splitter.sizes()
        if not sizes or not all(s >= 0 for s in sizes) or not any(s > 0 for s in sizes):
            return
        self._desired_body_sizes = list(sizes)
        self.body_sizes_changed.emit(list(sizes))
        # 兼容旧持久化字段：单独再记一次提交区高度
        idx = self.body_splitter.indexOf(self.commit_widget)
        if 0 <= idx < len(sizes) and sizes[idx] > 0:
            self._desired_commit_height = sizes[idx]
            self.commit_height_changed.emit(sizes[idx])

    def apply_commit_height(self, height: int):
        """由主窗口在加载配置后调用：设定要恢复的提交区高度。"""
        if isinstance(height, int) and height > 0:
            self._desired_commit_height = height
            self._apply_commit_height()

    def apply_body_sizes(self, sizes):
        """由主窗口在加载配置后调用：设定要恢复的 body 各栏高度。"""
        if isinstance(sizes, list) and sizes and all(isinstance(s, int) and s >= 0 for s in sizes):
            self._desired_body_sizes = list(sizes)
            self._apply_body_sizes()

    def _apply_body_sizes(self, _attempts: int = 0):
        """把记忆的 body 各栏高度套用到分隔器；栏数变化时按比例缩放再回填。"""
        sizes = self._desired_body_sizes
        if not sizes:
            return
        total = self.body_splitter.height()
        if total <= 1:
            if _attempts < 40:
                QTimer.singleShot(50, lambda: self._apply_body_sizes(_attempts + 1))
            return
        cur = self.body_splitter.sizes()
        n = len(cur)
        if n == 0:
            return
        # 栏数对得上就直接用；对不上则按比例填充到当前栏数
        if len(sizes) == n:
            src = list(sizes)
        else:
            src = (list(sizes) + [max(1, sum(sizes) // max(1, len(sizes)))] * n)[:n]
        s_total = sum(src) or 1
        new_sizes = [max(1, int(total * s / s_total)) for s in src]
        self.body_splitter.setSizes(new_sizes)

    def _apply_commit_height(self, _attempts: int = 0):
        """把记忆的提交区高度套用到分隔器（提交区在 splitter 中的实际索引），
        其余栏按原比例分剩余空间；布局还没就绪时稍后重试。"""
        height = self._desired_commit_height
        if height <= 0:
            return
        total = self.body_splitter.height()
        if total <= 1:
            # 面板还没排好版，等一下再试
            if _attempts < 40:
                QTimer.singleShot(50, lambda: self._apply_commit_height(_attempts + 1))
            return
        sizes = self.body_splitter.sizes()
        idx = self.body_splitter.indexOf(self.commit_widget)
        if idx < 0 or idx >= len(sizes):
            return
        commit = max(80, min(height, total - 120))
        rest = max(1, total - commit)
        other_sizes = [s for i, s in enumerate(sizes) if i != idx]
        other_total = sum(other_sizes) or 1
        # 其余栏按当前比例分摊剩余空间，再把 commit 塞回原索引
        scaled = [max(1, int(rest * s / other_total)) for s in other_sizes]
        new_sizes = scaled[:idx] + [commit] + scaled[idx:]
        # setSizes 不会触发 splitterMoved，不会回写循环
        self.body_splitter.setSizes(new_sizes)

    def _connect_signals(self):
        """连接信号"""
        # Git 管理器信号
        self._git_manager.status_changed.connect(self._refresh_status)
        self._git_manager.error_occurred.connect(self._show_error)
        self._git_manager.status_ok.connect(self._on_status_ok)
        self._git_manager.op_output.connect(self._on_op_output)

        # 头部信号
        self.header.ref_changed.connect(self._on_ref_changed)
        self.header.refresh_clicked.connect(self._on_refresh_clicked)
        self.header.stash_clicked.connect(self._on_stash_clicked)
        self.header.settings_clicked.connect(self._on_settings_clicked)
        self.header.delete_branch_requested.connect(self._on_delete_branch)

        # 变更列表信号
        self.changes_widget.stage_file.connect(self._git_manager.stage_file)
        self.changes_widget.unstage_file.connect(self._git_manager.unstage_file)
        self.changes_widget.discard_file.connect(self._on_discard_file)
        self.changes_widget.stage_all.connect(self._git_manager.stage_all)
        self.changes_widget.unstage_all.connect(self._git_manager.unstage_all)
        self.changes_widget.view_diff.connect(self._show_diff)
        self.changes_widget.resolve_ours.connect(
            lambda path: self._on_resolve_conflict(path, 'ours'))
        self.changes_widget.resolve_theirs.connect(
            lambda path: self._on_resolve_conflict(path, 'theirs'))
        self.changes_widget.mark_resolved.connect(self._on_mark_resolved)

        # 提交信号
        self.commit_widget.commit_requested.connect(self._on_commit)
        self.commit_widget.push_requested.connect(self._on_push)
        self.commit_widget.pull_requested.connect(self._on_pull)
        self.commit_widget.generate_requested.connect(self._on_generate_message)

    def set_repository(self, path: str):
        """设置仓库路径"""
        is_repo = self._git_manager.set_repository(path)

        if is_repo:
            self.no_repo_label.hide()
            self.header.show()
            self.changes_widget.show()
            self.graph_widget.show()
            self.commit_widget.show()
            # set_repository 会无条件启动 5s 轮询；若此刻面板不可见，立刻停掉，
            # 否则隐藏状态下不会再有 hideEvent 来停它（复显时 showEvent 会恢复）。
            if not self.isVisible():
                self._git_manager.pause_polling()
            self._refresh_all_async()
            # 面板每次显示时，恢复用户记忆的各栏高度
            if self._desired_body_sizes:
                self._apply_body_sizes()
            else:
                self._apply_commit_height()
            # 后台抓一次远程，刷新"可 pull 条数"
            self._fetch_async()
        else:
            self.no_repo_label.show()
            self.header.hide()
            self.merge_banner.hide()
            self.changes_widget.hide()
            self.graph_widget.hide()
            self.commit_widget.hide()

    def _on_commit_clicked(self, commit_hash: str):
        """点击 graph 上的提交 → 在右侧大空间展示该提交详情（git show）。"""
        text = self._git_manager.get_commit_show(commit_hash)
        self.output_requested.emit(
            t("git.commit_show_title", short=commit_hash[:7]), text
        )

    def _on_revert_commit(self, commit_hash: str):
        """右键菜单：撤销某次提交（git revert，安全、不改写历史）。"""
        short = commit_hash[:7]
        reply = QMessageBox.question(
            self,
            t("git.confirm_revert_title"),
            t("git.confirm_revert_msg", short=short),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._git_manager.revert_commit(commit_hash):
            self._refresh_all_async()

    def _on_reset_commit(self, commit_hash: str, mode: str):
        """右键菜单：重置当前分支到某次提交（git reset --<mode>，会改写本地历史）。"""
        short = commit_hash[:7]
        msg_key = {
            'soft': 'git.confirm_reset_soft_msg',
            'mixed': 'git.confirm_reset_mixed_msg',
            'hard': 'git.confirm_reset_hard_msg',
        }.get(mode, 'git.confirm_reset_mixed_msg')
        reply = QMessageBox.question(
            self,
            t("git.confirm_reset_title"),
            t(msg_key, short=short),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._git_manager.reset_to_commit(commit_hash, mode):
            self._refresh_all_async()

    def _on_copy_commit_hash(self, commit_hash: str):
        """右键菜单：复制提交完整哈希到剪贴板。"""
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(commit_hash)

    def _refresh_all_async(self):
        """后台收集 status/branches/log 并一次性应用，不阻塞 UI（首次 ⌘G 秒开）。

        合并策略：已有刷新在跑时只置 pending，跑完再补一次，避免叠 worker。
        """
        if self._git_manager._repo_path is None:
            return
        if self._refresh_running:
            self._refresh_pending = True
            return
        self._refresh_running = True
        worker = _RefreshWorker(self._git_manager,
                                log_limit=self.graph_widget.commit_count(),
                                parent=self)
        worker.loaded.connect(self._apply_refresh)
        worker.finished.connect(self._on_refresh_all_finished)
        self._register_worker(worker)
        worker.start()

    def _on_refresh_all_finished(self):
        self._refresh_running = False
        if self._refresh_pending:
            self._refresh_pending = False
            self._refresh_all_async()

    def _apply_refresh(self, data: dict):
        """在 UI 线程应用后台收集到的刷新数据。"""
        if not data.get('ok'):
            return
        self.changes_widget.update_files(data['staged'], data['unstaged'])
        self.commit_widget.set_ahead_behind(data['ahead'], data['behind'])
        self.header.update_branches(data['branches'], data['head_ref'], data['tags'])
        limit = data.get('log_limit', 150)
        self.graph_widget.set_commits(data['commits'],
                                      has_more=len(data['commits']) >= limit)
        self._update_merge_banner(data['unstaged'], merging=data.get('merging', False))

    def _on_load_more_commits(self, skip: int):
        """graph 滚动到底 → 后台取下一页提交并追加（每页 150 条）。"""
        if self._log_page_running or self._git_manager._repo_path is None:
            return
        self._log_page_running = True
        worker = _LogPageWorker(self._git_manager, skip, 150, self)
        worker.loaded.connect(self._apply_log_page)
        worker.finished.connect(
            lambda: setattr(self, '_log_page_running', False))
        self._register_worker(worker)
        worker.start()

    def _apply_log_page(self, commits: list, skip: int):
        """在 UI 线程把下一页提交追加进 graph。取满一页说明后面可能还有。"""
        self.graph_widget.append_commits(commits, skip,
                                         has_more=len(commits) >= 150)

    def _refresh_status(self):
        """刷新文件状态（轻量：status + ahead/behind + merge 态，后台线程收集）。

        - 面板不可见时跳过（5s 定时器照常触发，但没必要为看不见的面板
          spawn git 子进程占用 UI 资源），重新可见时 showEvent 补一次；
        - 数据收集在 _StatusWorker 线程，避免 Windows 上 30-100ms/个的
          子进程 spawn 把 UI 线程卡出每 5 秒一顿的节奏。
        """
        if not self.isVisible():
            self._status_stale = True
            return
        if self._status_refresh_running:
            return
        self._status_refresh_running = True
        worker = _StatusWorker(self._git_manager, self)
        worker.loaded.connect(self._apply_status_refresh)
        worker.finished.connect(
            lambda: setattr(self, '_status_refresh_running', False))
        self._register_worker(worker)
        worker.start()

    def _apply_status_refresh(self, data: dict):
        """在 UI 线程应用轻量状态刷新的数据。"""
        if not data.get('ok'):
            return
        self.changes_widget.update_files(data['staged'], data['unstaged'])
        self.commit_widget.set_ahead_behind(data['ahead'], data['behind'])
        self._update_merge_banner(data['unstaged'],
                                  merging=data.get('merging', False))

    def showEvent(self, event):
        super().showEvent(event)
        # 面板重新可见：恢复 5 秒备份轮询，并补一次刷新。
        # 无条件刷新（不只看 _status_stale）：隐藏期间轮询被停掉了，文件监视器
        # 万一漏掉某次变动，复显时也能立刻对齐到最新状态。
        self._git_manager.resume_polling()
        self._status_stale = False
        self._refresh_status()

    def hideEvent(self, event):
        super().hideEvent(event)
        # 面板不可见：停掉 5 秒备份轮询，省掉空转（隐藏期间 _refresh_status 本就早退，
        # 不会 spawn git；文件监视器仍在，复显时 showEvent 补刷）。
        self._git_manager.pause_polling()

    def _update_merge_banner(self, unstaged: list, merging: bool = None):
        """根据 merge 状态 / 未解决冲突数更新提示条。

        - 合并进行中：显示"合并进行中"提示 + 中止合并按钮
        - 非合并但有冲突（如 stash pop / cherry-pick 产生）：只显示冲突数
        - 都没有：隐藏
        """
        if merging is None:
            merging = self._git_manager.is_merging()
        n_conflicts = sum(1 for f in unstaged if getattr(f, 'is_conflict', False))
        if merging:
            if n_conflicts:
                self.merge_label.setText(t("git.merge_with_conflicts", n=n_conflicts))
            else:
                self.merge_label.setText(t("git.merge_in_progress"))
            self.abort_merge_btn.setVisible(True)
            self.merge_banner.setVisible(True)
        elif n_conflicts:
            self.merge_label.setText(t("git.conflicts_present", n=n_conflicts))
            self.abort_merge_btn.setVisible(False)
            self.merge_banner.setVisible(True)
        else:
            self.merge_banner.setVisible(False)

    def _on_abort_merge(self):
        """中止合并（git merge --abort），二次确认后执行。"""
        reply = QMessageBox.question(
            self,
            t("git.merge_abort_confirm_title"),
            t("git.merge_abort_confirm_msg"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._git_manager.merge_abort():
            self._refresh_all_async()

    def _on_resolve_conflict(self, path: str, side: str):
        """冲突文件右键：整体采用我方/对方版本（checkout --ours/--theirs + add）。"""
        key = "git.confirm_resolve_ours_msg" if side == 'ours' else "git.confirm_resolve_theirs_msg"
        reply = QMessageBox.question(
            self,
            t("git.confirm_resolve_title"),
            t(key, path=path),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._git_manager.resolve_conflict_with(path, side):
            self._refresh_all_async()

    def _on_mark_resolved(self, path: str):
        """冲突文件右键：标记为已解决（git add，假定用户已手工编辑掉冲突标记）。"""
        if self._git_manager.stage_file(path):
            self._refresh_all_async()

    def _on_refresh_clicked(self):
        """点击 ↻：刷新状态 + 强制抓取一次远程（更新可 pull 条数）"""
        self._refresh_all_async()
        self._fetch_async(force=True)

    def _on_create_branch(self):
        """点击 +：从当前 HEAD 创建新分支并切换过去。"""
        text, ok = QInputDialog.getText(
            self,
            t("git.create_branch_title"),
            t("git.create_branch_prompt"),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            return
        name = (text or '').strip()
        if not name:
            return
        if self._git_manager.create_branch(name):
            self._refresh_all_async()

    def _on_delete_branch(self, name: str):
        """右键菜单确认删除本地分支。

        流程：
        1. 与当前 HEAD 同名时直接拒绝（双保险，header 也已过滤）。
        2. 先弹一次性确认。
        3. 用 `git branch -d` 安全删除；若 git 报 "not fully merged"，
           再弹一次"强制删除"二次确认，改用 `-D`。
        """
        name = (name or '').strip()
        if not name:
            return
        # 双保险：拒绝删除当前分支
        cur = self._git_manager.get_current_branch()
        if cur and cur == name:
            QMessageBox.warning(
                self,
                t("git.delete_branch_title"),
                t("git.delete_branch_current_msg", name=name),
            )
            return

        reply = QMessageBox.question(
            self,
            t("git.delete_branch_title"),
            t("git.delete_branch_confirm_msg", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        ok, output = self._git_manager.delete_branch(name, force=False)
        if ok:
            self._refresh_all_async()
            return

        # 未合并 → 询问是否强制删除
        if 'not fully merged' in (output or '').lower():
            force_reply = QMessageBox.warning(
                self,
                t("git.delete_branch_title"),
                t("git.delete_branch_force_msg", name=name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if force_reply != QMessageBox.StandardButton.Yes:
                return
            ok, output = self._git_manager.delete_branch(name, force=True)
            if ok:
                self._refresh_all_async()
                return

        QMessageBox.critical(
            self,
            t("git.error_title"),
            t("git.delete_branch_failed_msg", error=(output or '').strip()),
        )

    # ---------- Git 设置（代理等） ----------

    def _load_config(self) -> dict:
        return app_config.read_config()

    def _save_config(self, patch: dict):
        """把 patch 合并写回主配置文件（app_config 单点：锁 + 原子写 + 失败日志）。"""
        app_config.update_config(patch, description='git-panel')

    def _apply_persisted_git_proxy(self):
        """启动时从配置文件读取 git_proxy 并应用到 GitManager。

        历史列表保存在 git_proxies（list[str]），当前激活的在 git_proxy（str）。
        - 启动时把历史 / 激活值统一归一化（旧版可能存了不带 http:// 的裸地址）。
        - 若当前激活不在历史里，自动补进历史。
        """
        cfg = self._load_config()
        active = self._normalize_proxy(cfg.get('git_proxy') or '')
        history_raw = cfg.get('git_proxies') or []
        history = self._sanitize_proxy_history(
            [self._normalize_proxy(x) for x in history_raw]
        )
        patch = {}
        if active and active not in history:
            history.append(active)
        # 仅在归一化后真正变化时写回，避免无谓的文件写
        if active != (cfg.get('git_proxy') or '').strip():
            patch['git_proxy'] = active
        if history != history_raw:
            patch['git_proxies'] = history
        if patch:
            self._save_config(patch)
        self._git_manager.set_proxy(active)

    @staticmethod
    def _sanitize_proxy_history(items) -> list:
        """去重 + 去空白 + 保持原顺序。"""
        if not isinstance(items, list):
            return []
        out = []
        seen = set()
        for x in items:
            s = str(x or '').strip()
            if s and s not in seen:
                out.append(s)
                seen.add(s)
        return out

    def _get_proxy_history(self) -> list:
        return self._sanitize_proxy_history(self._load_config().get('git_proxies') or [])

    @staticmethod
    def _normalize_proxy(url: str) -> str:
        """补全 proxy URL：缺 scheme 时自动加 http://。

        - 'http://127.0.0.1:7897'      → 不变
        - '127.0.0.1:7897'             → 'http://127.0.0.1:7897'
        - 'socks5://127.0.0.1:1080'    → 不变（其它 scheme 不动）
        - 空串                          → 空串

        中文输入法很容易打出全角字符（如 'http://127.0.0.1：7897' 里的全角
        冒号 U+FF1A），git/curl 会报 "URL using bad/illegal format" 导致
        push/pull 全部失败，这里统一折算成半角。启动时 _apply_persisted_git_proxy
        会用归一化结果回写配置，历史上存坏的值也能自愈。
        """
        url = (url or '').strip()
        if not url:
            return ''
        # 全角 ASCII 区（U+FF01–U+FF5E）→ 半角；全角空格 → 普通空格
        url = url.translate(
            {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}
        ).strip()
        if not url:
            return ''
        if '://' in url:
            return url
        return 'http://' + url

    def _apply_proxy_choice(self, url: str):
        """切换激活代理：写 GitManager + 持久化（输入会被归一化）。"""
        url = self._normalize_proxy(url)
        self._git_manager.set_proxy(url)
        patch = {'git_proxy': url}
        if url:
            history = self._get_proxy_history()
            if url not in history:
                history.append(url)
                patch['git_proxies'] = history
        self._save_config(patch)

    def _on_settings_clicked(self):
        """齿轮按钮 → 弹出快速切换菜单。"""
        active = self._git_manager.get_proxy()
        history = self._get_proxy_history()

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 24px;
            }}
            QMenu::item:selected {{
                background-color: {self.theme.get('accent', '#667eea')};
            }}
        """)

        # 新建分支（从 header 的 + 按钮移到这里，避免误点意外建分支）
        act_new_branch = QAction(t("git.menu_create_branch"), self)
        act_new_branch.triggered.connect(self._on_create_branch)
        menu.addAction(act_new_branch)
        menu.addSeparator()

        # 所有 proxy 相关项收进一个 "Proxy" 子菜单，保持顶层菜单简洁
        proxy_menu = menu.addMenu(t("git.menu_proxy"))
        proxy_menu.setStyleSheet(menu.styleSheet())
        # 当前选中的代理显示在子菜单标题旁，不展开也能看到
        proxy_menu.setTitle(t("git.menu_proxy") + (f"  ({active})" if active else ""))

        # (No proxy) 项
        act_none = QAction(t("git.proxy_none"), self)
        act_none.setCheckable(True)
        act_none.setChecked(active == '')
        act_none.triggered.connect(lambda: self._apply_proxy_choice(''))
        proxy_menu.addAction(act_none)

        # 历史代理
        if history:
            proxy_menu.addSeparator()
            for url in history:
                act = QAction(url, self)
                act.setCheckable(True)
                act.setChecked(url == active)
                act.triggered.connect(lambda _checked, u=url: self._apply_proxy_choice(u))
                proxy_menu.addAction(act)

        # 添加 / 管理
        proxy_menu.addSeparator()
        act_add = QAction(t("git.proxy_add_new"), self)
        act_add.triggered.connect(self._add_new_proxy)
        proxy_menu.addAction(act_add)

        if history:
            act_manage = QAction(t("git.proxy_manage"), self)
            act_manage.triggered.connect(self._manage_proxies)
            proxy_menu.addAction(act_manage)

        # 紧贴齿轮按钮下方弹出
        btn = self.header.settings_btn
        pos = btn.mapToGlobal(QPoint(0, btn.height()))
        menu.exec(pos)

    def _add_new_proxy(self):
        """弹出输入框添加新代理，加入历史并激活。"""
        text, ok = QInputDialog.getText(
            self,
            t("git.proxy_add_title"),
            t("git.proxy_add_prompt"),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            return
        url = (text or '').strip()
        if not url:
            return
        self._apply_proxy_choice(url)

    def _manage_proxies(self):
        """管理代理历史：列表 + 编辑/删除按钮。

        - 编辑：弹输入框预填当前 URL；保存时归一化、保持原顺序、并同步当前激活。
        - 删除：从历史移除；若是当前激活则切到 (No proxy)。
        - 双击列表项 = 快捷编辑。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(t("git.proxy_manage_title"))
        dlg.setMinimumWidth(420)
        v = QVBoxLayout(dlg)

        list_widget = QListWidget(dlg)
        for url in self._get_proxy_history():
            QListWidgetItem(url, list_widget)
        v.addWidget(list_widget)

        btns = QHBoxLayout()
        edit_btn = QPushButton(t("git.proxy_edit_btn"))
        remove_btn = QPushButton(t("git.proxy_remove_btn"))
        close_btn = QPushButton(t("git.proxy_close_btn"))
        btns.addWidget(edit_btn)
        btns.addWidget(remove_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        v.addLayout(btns)

        def _persist_from_list(old_url: str = '', new_url: str = ''):
            """从列表当前内容回写配置；如果 old→new 是当前激活的修改，同步激活值。"""
            history = [list_widget.item(i).text() for i in range(list_widget.count())]
            patch = {'git_proxies': history}
            active = self._git_manager.get_proxy()
            if old_url and old_url == active:
                # 当前激活被改名/删除
                self._git_manager.set_proxy(new_url)
                patch['git_proxy'] = new_url
            self._save_config(patch)

        def do_remove():
            item = list_widget.currentItem()
            if not item:
                return
            url = item.text()
            row = list_widget.row(item)
            list_widget.takeItem(row)
            _persist_from_list(old_url=url, new_url='')

        def do_edit():
            item = list_widget.currentItem()
            if not item:
                return
            old = item.text()
            text, ok = QInputDialog.getText(
                dlg,
                t("git.proxy_edit_title"),
                t("git.proxy_add_prompt"),
                QLineEdit.EchoMode.Normal,
                old,
            )
            if not ok:
                return
            new = self._normalize_proxy(text)
            if not new or new == old:
                return
            # 去重：若新值已存在于其他行，删除原行并选中已存在那行
            for i in range(list_widget.count()):
                if i != list_widget.row(item) and list_widget.item(i).text() == new:
                    item_row = list_widget.row(item)
                    list_widget.takeItem(item_row)
                    # takeItem 后 item 已移除，须用移除前缓存的行号判断
                    list_widget.setCurrentRow(i if i < item_row else i - 1)
                    _persist_from_list(old_url=old, new_url=new)
                    return
            item.setText(new)
            _persist_from_list(old_url=old, new_url=new)

        edit_btn.clicked.connect(do_edit)
        remove_btn.clicked.connect(do_remove)
        close_btn.clicked.connect(dlg.accept)
        list_widget.itemDoubleClicked.connect(lambda _it: do_edit())
        dlg.exec()

    # ---------- Stash ----------

    def _on_stash_clicked(self):
        """Stash 按钮 → 弹出菜单：贮藏当前修改 / 管理已有 stash。"""
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 24px;
            }}
            QMenu::item:selected {{
                background-color: {self.theme.get('accent', '#667eea')};
            }}
        """)

        act_save = QAction(t("git.stash_save_menu"), self)
        act_save.triggered.connect(self._on_stash_save)
        menu.addAction(act_save)

        act_manage = QAction(t("git.stash_manage_menu"), self)
        act_manage.triggered.connect(self._show_stash_dialog)
        menu.addAction(act_manage)

        # 紧贴 Stash 按钮下方弹出（与齿轮菜单一致）
        btn = self.header.stash_btn
        pos = btn.mapToGlobal(QPoint(0, btn.height()))
        menu.exec(pos)

    def _on_stash_save(self):
        """贮藏当前修改（含未跟踪文件），说明可留空。"""
        text, ok = QInputDialog.getText(
            self,
            t("git.stash_save_title"),
            t("git.stash_save_prompt"),
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            return
        success, output = self._git_manager.stash_save(text)
        if not success:
            return  # 失败信息已由 error_occurred 弹出
        if 'no local changes' in (output or '').lower():
            QMessageBox.information(
                self,
                t("git.stash_nothing_title"),
                t("git.stash_nothing_msg"),
            )
            return
        self._refresh_all_async()

    def _show_stash_dialog(self):
        """Stash 管理对话框：列出现有 stash，支持 Pop / Apply / Drop。

        stash 操作均为本地命令（与 commit/checkout 一样同步执行），
        操作完成后就地刷新列表并触发面板刷新。
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(t("git.stash_manage_title"))
        dlg.setMinimumWidth(480)
        v = QVBoxLayout(dlg)

        list_widget = QListWidget(dlg)
        v.addWidget(list_widget)

        btns = QHBoxLayout()
        pop_btn = QPushButton(t("git.stash_pop_btn"))
        apply_btn = QPushButton(t("git.stash_apply_btn"))
        drop_btn = QPushButton(t("git.stash_drop_btn"))
        close_btn = QPushButton(t("git.stash_close_btn"))
        btns.addWidget(pop_btn)
        btns.addWidget(apply_btn)
        btns.addWidget(drop_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        v.addLayout(btns)

        def reload_list():
            list_widget.clear()
            stashes = self._git_manager.stash_list()
            for st in stashes:
                branch = f" [{st.branch}]" if st.branch else ""
                item = QListWidgetItem(
                    f"{st.ref}{branch}  {st.message}  ({st.date})", list_widget
                )
                item.setData(Qt.ItemDataRole.UserRole, st.index)
            has_any = bool(stashes)
            if not has_any:
                placeholder = QListWidgetItem(t("git.stash_empty"), list_widget)
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            for b in (pop_btn, apply_btn, drop_btn):
                b.setEnabled(has_any)

        def current_index():
            item = list_widget.currentItem()
            if item is None:
                return None
            return item.data(Qt.ItemDataRole.UserRole)

        def do_op(op: str):
            idx = current_index()
            if idx is None:
                return
            if op == 'drop':
                reply = QMessageBox.question(
                    dlg,
                    t("git.stash_drop_confirm_title"),
                    t("git.stash_drop_confirm_msg", ref=f"stash@{{{idx}}}"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                ok = self._git_manager.stash_drop(idx)
            elif op == 'pop':
                ok = self._git_manager.stash_pop(idx)
            else:
                ok = self._git_manager.stash_apply(idx)
            # pop/apply 失败（如产生冲突）时 stash 仍在，错误已弹窗；
            # 无论成败都重载列表 + 刷新面板，保证 UI 与仓库实际状态一致。
            reload_list()
            self._refresh_all_async()

        pop_btn.clicked.connect(lambda: do_op('pop'))
        apply_btn.clicked.connect(lambda: do_op('apply'))
        drop_btn.clicked.connect(lambda: do_op('drop'))
        close_btn.clicked.connect(dlg.accept)

        reload_list()
        dlg.exec()

    def _tick_fetch(self):
        """定时器：仅在面板可见时后台 fetch，更新可 pull 条数。"""
        if self.isVisible():
            self._fetch_async()

    def _fetch_async(self, force: bool = False):
        """后台 git fetch（不阻塞 UI、不弹窗）。默认 60s 内只抓一次，避免频繁联网。"""
        import time
        if self._git_manager._repo_path is None:
            return
        if self._fetch_running:
            return
        now = time.monotonic()
        if not force and (now - self._last_fetch_ts) < 60:
            return
        self._last_fetch_ts = now
        self._fetch_running = True
        worker = _GitOpWorker(self._git_manager.fetch, 'fetch', self)
        worker.done.connect(self._on_fetch_done)
        worker.finished.connect(self._on_fetch_finished)
        self._register_worker(worker)
        worker.start()

    def _on_fetch_finished(self):
        self._fetch_running = False

    def _on_fetch_done(self, ok: bool, _kind: str):
        # 抓取成功后远程跟踪分支已更新 → 重算 ahead/behind，刷新 Pull 计数 + graph
        if ok:
            self._refresh_all_async()

    def _register_worker(self, worker):
        """登记后台线程：跑完自动从集合移除并 deleteLater；关闭时统一等待。"""
        self._active_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._active_workers.discard(w))
        worker.finished.connect(worker.deleteLater)

    def shutdown(self):
        """关闭前调用：停掉定时器并等待在跑的后台线程，避免线程仍在运行时被销毁导致 abort。

        关键：先 kill 正在跑的 git 子进程，否则 push/pull 卡在网络上时
        worker 一直阻塞在 subprocess.communicate()，QThread.terminate() 对
        阻塞在 C 库里的 Python 线程不可靠，会导致整个程序无法退出。
        """
        try:
            self._fetch_timer.stop()
        except RuntimeError:
            pass
        # 先放掉子进程，worker 线程随即从 communicate() 返回，wait() 几乎瞬间完成
        try:
            self._git_manager.cancel_running()
        except Exception:
            pass
        for worker in list(self._active_workers):
            try:
                if worker.isRunning():
                    if not worker.wait(3000):
                        worker.terminate()
                        worker.wait(1000)
            except RuntimeError:
                pass
        self._active_workers.clear()

    def _on_ref_changed(self, kind: str, name: str):
        """引用切换处理（本地/远程分支或 tag）。

        checkout 在后台线程执行：大仓库/慢磁盘时同步跑会冻结整个窗口
        （_run_git 超时上限 30s）。
        """
        # detached 占位项仅用于显示，不触发任何操作
        if kind == 'detached':
            return
        head_kind, head_name = self._git_manager.get_head_ref()
        # 已经在目标引用上，无需切换
        if (kind, name) == (head_kind, head_name):
            return
        if self._checkout_running:
            # 上一个 checkout 还没完成：忽略本次，刷新让选中态回到实际 HEAD
            self._refresh_all_async()
            return
        self._checkout_running = True
        worker = _GitOpWorker(
            lambda: self._git_manager.checkout_ref(kind, name), 'checkout', self)
        worker.done.connect(self._on_checkout_done)
        self._register_worker(worker)
        worker.start()

    def _on_checkout_done(self, _ok: bool, _kind: str):
        self._checkout_running = False
        # 不论 checkout 成功失败都刷新一次（含状态），以同步 UI 选中态
        self._refresh_all_async()

    def _on_discard_file(self, path: str):
        """放弃更改确认"""
        reply = QMessageBox.question(
            self,
            t("git.confirm_discard_title"),
            t("git.confirm_discard_msg", path=path),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._git_manager.discard_changes(path)

    def _on_commit(self, message: str):
        """提交处理：仅在提交成功后才清空输入框，失败时保留用户已写的信息。

        commit 在后台线程执行：pre-commit hook / 大索引可能耗时数秒到
        数十秒，同步跑会冻结整个窗口（_run_git 超时上限 30s）。
        """
        if self._commit_running:
            return
        self._commit_running = True
        self.commit_widget.set_busy('commit', True)
        worker = _GitOpWorker(
            lambda: self._git_manager.commit(message), 'commit', self)
        worker.done.connect(self._on_commit_done)
        self._register_worker(worker)
        worker.start()

    def _on_commit_done(self, ok: bool, _kind: str):
        self._commit_running = False
        self.commit_widget.set_busy('commit', False)
        if ok:
            self.commit_widget.clear_message()
            self._refresh_all_async()

    def _on_push(self):
        """推送处理（后台线程，避免网络阻塞卡死 UI）"""
        self._run_git_op_async('push')

    def _on_pull(self):
        """拉取处理（后台线程，避免网络阻塞卡死 UI）"""
        self._run_git_op_async('pull')

    def _run_git_op_async(self, kind: str):
        # 操作期间对应按钮已被 set_busy 禁用，足以防重复点击
        self.commit_widget.set_busy(kind, True)
        fn = self._git_manager.push if kind == 'push' else self._git_manager.pull
        worker = _GitOpWorker(fn, kind, self)
        worker.done.connect(self._on_git_op_done)
        self._register_worker(worker)
        worker.start()

    def _on_git_op_done(self, ok: bool, kind: str):
        self.commit_widget.set_busy(kind, False)
        if not ok:
            # 失败信息已由 GitManager.error_occurred 弹出
            return
        # 成功不再弹窗打扰：push 后 ahead 计数归零、pull 后列表刷新，按钮本身就是反馈
        self._refresh_all_async()

    # ---------- ✨ 用大模型生成提交信息 ----------

    def _on_generate_message(self):
        """根据当前改动调用大模型生成提交信息，填进输入框。"""
        diff = self._collect_diff_for_message()
        if not diff.strip():
            QMessageBox.information(
                self, t("git.generate_no_changes_title"),
                t("git.generate_no_changes_msg")
            )
            return

        main_window = self._find_main_window()
        # 优先用「设为 Git 模型」指派的配置，否则回退默认配置
        config = None
        if main_window is not None:
            if hasattr(main_window, 'get_git_llm_config'):
                config = main_window.get_git_llm_config()
            else:
                config = main_window.get_llm_config()
        if not config or not config.get('api_base') or not config.get('model'):
            QMessageBox.warning(
                self, t("git.generate_no_config_title"),
                t("git.generate_no_config_msg")
            )
            return

        self.commit_widget.set_generating(True)
        worker = _CommitMessageWorker(config, diff, get_language(), self)
        worker.succeeded.connect(self._on_generate_done)
        worker.failed.connect(self._on_generate_failed)
        worker.finished.connect(
            lambda: (self.commit_widget.set_generating(False),
                     self._notify_generation_attention())
        )
        self._register_worker(worker)
        worker.start()

    def _on_generate_done(self, message: str):
        if message:
            self.commit_widget.set_message(message)

    def _notify_generation_attention(self):
        """生成提交信息完成（成功或失败）：窗口不在前台时点亮导航绿点，
        让切去别的窗口等结果的用户知道可以回来了。"""
        win = self._find_main_window()
        if (win is not None and hasattr(win, '_request_nav_attention')
                and hasattr(win, 'isActiveWindow') and not win.isActiveWindow()):
            win._request_nav_attention()

    def _on_generate_failed(self, error: str):
        QMessageBox.warning(
            self, t("git.generate_failed_title"),
            t("git.generate_failed_msg", error=error)
        )

    def _collect_diff_for_message(self, max_chars: int = 16000) -> str:
        """收集要喂给模型的改动：优先暂存区 diff，没有就用工作区 diff，
        并附上 `git status --short` 摘要（这样新增/未跟踪文件也能体现）。"""
        gm = self._git_manager
        ok, staged = gm._run_git('diff', '--cached')
        body = staged if (ok and staged.strip()) else ''
        if not body.strip():
            ok2, unstaged = gm._run_git('diff')
            if ok2 and unstaged.strip():
                body = unstaged

        ok3, status = gm._run_git('status', '--short')
        status = status.strip() if ok3 else ''

        sections = []
        if status:
            sections.append("# Changed files (git status --short)\n" + status)
        if body.strip():
            sections.append("# Diff\n" + body)
        text = "\n\n".join(sections)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]..."
        return text

    def _find_main_window(self):
        """向上找到持有 LLM 配置的主窗口（get_llm_config）。"""
        w = self.parent()
        while w is not None:
            if hasattr(w, 'get_llm_config'):
                return w
            w = w.parent()
        win = self.window()
        if win is not None and hasattr(win, 'get_llm_config'):
            return win
        return None

    def _show_diff(self, path: str, staged: bool):
        """双击文件 → 交给主窗口在右侧大空间以左右并排方式显示 diff（不弹窗）"""
        diff_content = self._git_manager.get_diff(path, staged)
        title = path + (" (staged)" if staged else "")
        self.diff_requested.emit(title, diff_content, path, staged)

    def _on_op_output(self, kind: str, output: str):
        """pull 等操作的 git 输出 → 交给主窗口在右侧大空间展示（不弹窗）"""
        if kind == 'pull':
            self.output_requested.emit(t("git.pull_output_title"), output)

    def _show_error(self, message: str):
        """显示错误消息（同一条只弹一次）。

        状态刷新是 5s 定时 + 文件监视触发的轮询：当本地仓库目录被删/改名后，
        每次轮询 git status 都会失败并发同一条 error_occurred，若每次都弹 modal
        就会连珠不停。这里对相同文案去重，只在它「首次出现」时弹一次；下次刷新
        成功（_apply_*refresh 拿到 ok）会清掉去重标记，使日后真的换了别的错误
        （或同一错误再次发生）仍能再次提示。
        """
        if message == self._last_error_message:
            return
        self._last_error_message = message
        QMessageBox.warning(self, t("git.error_title"), message)

    def _on_status_ok(self):
        """get_status 真正成功 → 复位错误去重标记。

        这样仓库目录被删/改名时只弹一次警告；待用户恢复目录或切回正常仓库、
        status 重新成功后，标记清零，日后若再发生错误仍能再次提示一次。
        """
        self._last_error_message = None

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        self._update_style()
        self.header.apply_theme(theme)
        self.changes_widget.apply_theme(theme)
        self.graph_widget.apply_theme(theme)
        self.commit_widget.apply_theme(theme)

        self.no_repo_label.setStyleSheet(f"""
            QLabel {{
                color: {theme.get('text_dim', '#888')};
                font-size: 13px;
                padding: 20px;
            }}
        """)

    def apply_language(self):
        """更新语言相关的 UI 文本"""
        self.header.apply_language()
        self.changes_widget.apply_language()
        self.graph_widget.apply_language()
        self.commit_widget.apply_language()
        self.no_repo_label.setText(t("git.no_repo"))
        # 提示条标签文本由下一次状态刷新按当前语言重算，这里只更新按钮
        self.abort_merge_btn.setText(t("git.merge_abort_btn"))

    def refresh(self):
        """手动刷新"""
        self._refresh_all_async()
