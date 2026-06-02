"""本会话粘贴图片的缩略图画廊。

数据来源是终端每次粘贴图片时 emit 的真实文件路径（见 TerminalWidget.image_pasted），
因此不依赖 Claude Code 在 PTY 里渲染的 `[Image #N]` 占位文本——那段文本由子程序绘制，
我们无法可靠地映射回文件，所以用一份可靠的会话清单 + 缩略图来查看，更稳更直观。
"""

import os
import sys
import subprocess
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton,
    QScrollArea, QFrame, QMenu, QApplication,
)
from PyQt6.QtCore import Qt, QSize, QUrl
from PyQt6.QtGui import QPixmap, QDesktopServices

from flow_layout import FlowLayout
from i18n import t


THUMB_SIZE = 140  # 缩略图边长（像素）


def _reveal_in_file_manager(path: str):
    """在系统文件管理器中定位文件（macOS: Finder；其他平台尽力而为）。"""
    try:
        if sys.platform == 'darwin':
            subprocess.run(['open', '-R', path], check=False)
        elif sys.platform.startswith('win'):
            subprocess.run(['explorer', '/select,', os.path.normpath(path)], check=False)
        else:
            # Linux：退而求其次，打开所在目录
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
    except Exception:
        pass


class _Thumbnail(QFrame):
    """单张图片的缩略图卡片：双击打开，右键菜单提供更多操作。"""

    def __init__(self, path: str, on_removed, parent=None):
        super().__init__(parent)
        self._path = path
        self._on_removed = on_removed  # 回调：从列表移除后通知父窗口刷新
        self.setFixedWidth(THUMB_SIZE + 16)
        self.setObjectName("thumbCard")
        self.setStyleSheet("""
            QFrame#thumbCard {
                background-color: #1e1e2e;
                border: 1px solid #3d3d5c;
                border-radius: 6px;
            }
            QFrame#thumbCard:hover {
                border: 1px solid #6a5d8a;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        exists = os.path.isfile(path)

        self._image_label = QLabel()
        self._image_label.setFixedSize(THUMB_SIZE, THUMB_SIZE)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if exists:
            pix = QPixmap(path)
            if not pix.isNull():
                pix = pix.scaled(
                    QSize(THUMB_SIZE, THUMB_SIZE),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._image_label.setPixmap(pix)
            else:
                self._image_label.setText("?")
        else:
            self._image_label.setText("✕")
            self._image_label.setStyleSheet("color: #888;")
        layout.addWidget(self._image_label)

        name = Path(path).name
        caption = QLabel(name if exists else f"{name}\n{t('pasted_images.missing')}")
        caption.setWordWrap(True)
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        caption.setStyleSheet(
            "color: %s; font-size: 11px;" % ("#cccccc" if exists else "#888888")
        )
        caption.setFixedWidth(THUMB_SIZE)
        layout.addWidget(caption)

        self.setToolTip(path)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def mouseDoubleClickEvent(self, event):
        self._open()
        super().mouseDoubleClickEvent(event)

    def _open(self):
        if os.path.isfile(self._path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))

    def _show_menu(self, pos):
        menu = QMenu(self)
        act_open = menu.addAction(t("pasted_images.open"))
        act_reveal = menu.addAction(t("pasted_images.reveal"))
        act_copy = menu.addAction(t("pasted_images.copy_path"))
        menu.addSeparator()
        act_remove = menu.addAction(t("pasted_images.remove"))

        exists = os.path.isfile(self._path)
        act_open.setEnabled(exists)
        act_reveal.setEnabled(exists)

        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is act_open:
            self._open()
        elif chosen is act_reveal:
            _reveal_in_file_manager(self._path)
        elif chosen is act_copy:
            QApplication.clipboard().setText(self._path)
        elif chosen is act_remove:
            self._on_removed(self._path)


class PastedImagesDialog(QDialog):
    """展示本会话粘贴过的图片，支持双击打开 / 右键更多操作 / 清空列表。

    `images` 是按粘贴顺序排列的路径列表（最新的在后）；本窗口倒序展示（最新在前）。
    `on_clear` 在用户点击“清空列表”时调用，用于让调用方清空其会话记录。
    `on_remove` 在移除单张时调用。
    """

    def __init__(self, images, on_clear=None, on_remove=None, parent=None):
        super().__init__(parent)
        self._images = list(images)
        self._on_clear = on_clear
        self._on_remove = on_remove

        self.setWindowTitle(t("pasted_images.title"))
        self.resize(680, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # 顶部：数量 + 清空
        header = QHBoxLayout()
        self._count_label = QLabel()
        header.addWidget(self._count_label)
        header.addStretch()
        self._clear_btn = QPushButton(t("pasted_images.clear_all"))
        self._clear_btn.clicked.connect(self._clear_all)
        header.addWidget(self._clear_btn)
        root.addLayout(header)

        # 空状态提示
        self._empty_label = QLabel(t("pasted_images.empty"))
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet("color: #888; padding: 40px;")
        root.addWidget(self._empty_label)

        # 缩略图网格（流式布局 + 滚动）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._grid_host = QWidget()
        self._flow = FlowLayout(self._grid_host, h_spacing=10, v_spacing=10)
        self._scroll.setWidget(self._grid_host)
        root.addWidget(self._scroll, 1)

        self._rebuild()

    def _rebuild(self):
        """根据当前 _images 重建缩略图。"""
        # 清空旧缩略图
        while self._flow.count():
            item = self._flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        has_any = bool(self._images)
        self._empty_label.setVisible(not has_any)
        self._scroll.setVisible(has_any)
        self._clear_btn.setEnabled(has_any)
        self._count_label.setText(t("pasted_images.count", n=len(self._images)))

        # 最新的排在前面
        for path in reversed(self._images):
            self._flow.addWidget(_Thumbnail(path, self._remove_one, self._grid_host))

    def _remove_one(self, path: str):
        if path in self._images:
            self._images.remove(path)
        if callable(self._on_remove):
            self._on_remove(path)
        self._rebuild()

    def _clear_all(self):
        self._images = []
        if callable(self._on_clear):
            self._on_clear()
        self._rebuild()
