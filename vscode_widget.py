"""
VS Code 扩展面板 UI 组件
提供扩展搜索、安装、卸载的界面
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLabel,
    QPushButton, QLineEdit, QScrollArea, QListWidget,
    QListWidgetItem, QTabWidget, QMessageBox, QSizePolicy,
    QProgressBar
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QFont, QPixmap
import urllib.request

from vscode_manager import VSCodeManager, VSCodeExtension


class ExtensionItemWidget(QWidget):
    """扩展列表项组件"""

    install_clicked = pyqtSignal(str)       # 安装按钮点击
    uninstall_clicked = pyqtSignal(str)     # 卸载按钮点击

    def __init__(self, extension: VSCodeExtension, theme: dict = None, parent=None):
        super().__init__(parent)
        self.extension = extension
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        # 图标占位
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(40, 40)
        self.icon_label.setStyleSheet(f"""
            QLabel {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                border-radius: 6px;
            }}
        """)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setText("📦")
        layout.addWidget(self.icon_label)

        # 扩展信息
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)

        # 名称行
        name_layout = QHBoxLayout()
        name_layout.setSpacing(8)

        self.name_label = QLabel(self.extension.display_name)
        self.name_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text', '#eaeaea')};
                font-weight: bold;
                font-size: 13px;
            }}
        """)
        name_layout.addWidget(self.name_label)

        self.publisher_label = QLabel(self.extension.publisher)
        self.publisher_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text_dim', '#888')};
                font-size: 11px;
            }}
        """)
        name_layout.addWidget(self.publisher_label)
        name_layout.addStretch()

        info_layout.addLayout(name_layout)

        # 描述
        self.desc_label = QLabel(self.extension.short_description[:80] + "..." if len(self.extension.short_description) > 80 else self.extension.short_description)
        self.desc_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text_dim', '#888')};
                font-size: 11px;
            }}
        """)
        self.desc_label.setWordWrap(True)
        info_layout.addWidget(self.desc_label)

        # 统计信息
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(12)

        if self.extension.install_count > 0:
            install_text = self._format_count(self.extension.install_count)
            install_label = QLabel(f"⬇ {install_text}")
            install_label.setStyleSheet(f"color: {self.theme.get('text_dim', '#888')}; font-size: 10px;")
            stats_layout.addWidget(install_label)

        if self.extension.rating > 0:
            stars = "★" * int(self.extension.rating) + "☆" * (5 - int(self.extension.rating))
            rating_label = QLabel(f"{stars} {self.extension.rating:.1f}")
            rating_label.setStyleSheet(f"color: #f9a825; font-size: 10px;")
            stats_layout.addWidget(rating_label)

        version_label = QLabel(f"v{self.extension.version}")
        version_label.setStyleSheet(f"color: {self.theme.get('text_dim', '#888')}; font-size: 10px;")
        stats_layout.addWidget(version_label)

        stats_layout.addStretch()
        info_layout.addLayout(stats_layout)

        layout.addLayout(info_layout, 1)

        # 安装/卸载按钮
        if self.extension.is_installed:
            self.action_btn = QPushButton("卸载")
            self.action_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {self.theme.get('danger', '#ef4444')};
                    color: white;
                    border: none;
                    border-radius: 4px;
                    padding: 6px 12px;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background-color: {self.theme.get('danger_hover', '#dc2626')};
                }}
            """)
            self.action_btn.clicked.connect(lambda: self.uninstall_clicked.emit(self.extension.identifier))
        else:
            self.action_btn = QPushButton("安装")
            self.action_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {self.theme.get('success', '#4ade80')};
                    color: #000;
                    border: none;
                    border-radius: 4px;
                    padding: 6px 12px;
                    font-size: 12px;
                    font-weight: bold;
                }}
                QPushButton:hover {{
                    background-color: {self.theme.get('success_hover', '#22c55e')};
                }}
            """)
            self.action_btn.clicked.connect(lambda: self.install_clicked.emit(self.extension.identifier))

        layout.addWidget(self.action_btn)

    def _format_count(self, count: int) -> str:
        """格式化安装数"""
        if count >= 1000000:
            return f"{count / 1000000:.1f}M"
        elif count >= 1000:
            return f"{count / 1000:.1f}K"
        return str(count)

    def set_loading(self, loading: bool):
        """设置加载状态"""
        self.action_btn.setEnabled(not loading)
        if loading:
            self.action_btn.setText("...")


class InstalledExtensionItem(QWidget):
    """已安装扩展列表项"""

    uninstall_clicked = pyqtSignal(str)

    def __init__(self, extension_id: str, theme: dict = None, parent=None):
        super().__init__(parent)
        self.extension_id = extension_id
        self.theme = theme or {}
        self._setup_ui()

    def _setup_ui(self):
        """设置 UI"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(10)

        # 图标
        icon_label = QLabel("📦")
        layout.addWidget(icon_label)

        # 扩展 ID
        name_label = QLabel(self.extension_id)
        name_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text', '#eaeaea')};
                font-size: 12px;
            }}
        """)
        layout.addWidget(name_label, 1)

        # 卸载按钮
        uninstall_btn = QPushButton("卸载")
        uninstall_btn.setFixedSize(50, 24)
        uninstall_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('bg_lighter', '#3d3d5c')};
                color: {self.theme.get('text', '#eaeaea')};
                border: none;
                border-radius: 4px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('danger', '#ef4444')};
                color: white;
            }}
        """)
        uninstall_btn.clicked.connect(lambda: self.uninstall_clicked.emit(self.extension_id))
        layout.addWidget(uninstall_btn)


class VSCodeExtensionPanel(QWidget):
    """VS Code 扩展管理面板"""

    def __init__(self, theme: dict = None, parent=None):
        super().__init__(parent)
        self.theme = theme or {}
        self._manager = VSCodeManager(self)
        self._current_extensions: list = []

        self._setup_ui()
        self._connect_signals()

        # 延迟检查 VS Code 可用性
        QTimer.singleShot(100, self._check_vscode)

    def _setup_ui(self):
        """设置 UI"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 头部
        header = QFrame()
        header.setStyleSheet(f"""
            QFrame {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                border-bottom: 1px solid {self.theme.get('border', '#3d3d5c')};
            }}
        """)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(10, 8, 10, 8)
        header_layout.setSpacing(8)

        # 标题行
        title_layout = QHBoxLayout()
        title = QLabel("Extensions")
        title.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text', '#eaeaea')};
                font-weight: bold;
                font-size: 13px;
            }}
        """)
        title_layout.addWidget(title)
        title_layout.addStretch()

        # VS Code 状态
        self.vscode_status = QLabel()
        self.vscode_status.setStyleSheet(f"color: {self.theme.get('text_dim', '#888')}; font-size: 11px;")
        title_layout.addWidget(self.vscode_status)

        header_layout.addLayout(title_layout)

        # 搜索框
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索扩展...")
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
                color: {self.theme.get('text', '#eaeaea')};
                border: 1px solid {self.theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: {self.theme.get('accent', '#667eea')};
            }}
        """)
        self.search_input.returnPressed.connect(self._on_search)
        search_layout.addWidget(self.search_input)

        self.search_btn = QPushButton("搜索")
        self.search_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {self.theme.get('accent', '#667eea')};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {self.theme.get('accent_hover', '#7a8efa')};
            }}
        """)
        self.search_btn.clicked.connect(self._on_search)
        search_layout.addWidget(self.search_btn)

        header_layout.addLayout(search_layout)
        layout.addWidget(header)

        # 标签页
        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
            QTabBar::tab {{
                background-color: {self.theme.get('bg_medium', '#16213e')};
                color: {self.theme.get('text_dim', '#888')};
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }}
            QTabBar::tab:selected {{
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
                color: {self.theme.get('text', '#eaeaea')};
            }}
        """)

        # 搜索结果页
        self.search_scroll = QScrollArea()
        self.search_scroll.setWidgetResizable(True)
        self.search_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.search_scroll.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
        """)

        self.search_content = QWidget()
        self.search_layout = QVBoxLayout(self.search_content)
        self.search_layout.setContentsMargins(0, 0, 0, 0)
        self.search_layout.setSpacing(1)
        self.search_layout.addStretch()
        self.search_scroll.setWidget(self.search_content)

        # 搜索提示
        self.search_hint = QLabel("输入关键词搜索 VS Code 扩展")
        self.search_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.search_hint.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('text_dim', '#888')};
                font-size: 12px;
                padding: 40px;
            }}
        """)
        self.search_layout.insertWidget(0, self.search_hint)

        self.tab_widget.addTab(self.search_scroll, "搜索")

        # 已安装页
        self.installed_scroll = QScrollArea()
        self.installed_scroll.setWidgetResizable(True)
        self.installed_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.installed_scroll.setStyleSheet(f"""
            QScrollArea {{
                border: none;
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
        """)

        self.installed_content = QWidget()
        self.installed_layout = QVBoxLayout(self.installed_content)
        self.installed_layout.setContentsMargins(0, 0, 0, 0)
        self.installed_layout.setSpacing(1)
        self.installed_layout.addStretch()
        self.installed_scroll.setWidget(self.installed_content)

        self.tab_widget.addTab(self.installed_scroll, "已安装 (0)")

        layout.addWidget(self.tab_widget)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # 无限进度
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                border: none;
                background-color: {self.theme.get('bg_dark', '#1a1a2e')};
            }}
            QProgressBar::chunk {{
                background-color: {self.theme.get('accent', '#667eea')};
            }}
        """)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        # VS Code 不可用提示
        self.no_vscode_label = QLabel("VS Code 未安装或 'code' 命令不可用\n\n请安装 VS Code 并确保 'code' 命令在 PATH 中")
        self.no_vscode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.no_vscode_label.setStyleSheet(f"""
            QLabel {{
                color: {self.theme.get('danger', '#ef4444')};
                font-size: 12px;
                padding: 20px;
            }}
        """)
        self.no_vscode_label.hide()
        layout.addWidget(self.no_vscode_label)

    def _connect_signals(self):
        """连接信号"""
        self._manager.search_completed.connect(self._on_search_completed)
        self._manager.install_completed.connect(self._on_install_completed)
        self._manager.error_occurred.connect(self._show_error)
        self._manager.installed_list_updated.connect(self._update_installed_list)

        # Tab 切换时刷新已安装列表
        self.tab_widget.currentChanged.connect(self._on_tab_changed)

    def _check_vscode(self):
        """检查 VS Code 可用性"""
        if self._manager.is_vscode_available():
            version = self._manager.get_vscode_version()
            self.vscode_status.setText(f"VS Code {version}")
            self.no_vscode_label.hide()
            self.tab_widget.show()
            self._manager.refresh_installed()
        else:
            self.vscode_status.setText("VS Code 不可用")
            self.vscode_status.setStyleSheet(f"color: {self.theme.get('danger', '#ef4444')}; font-size: 11px;")
            self.no_vscode_label.show()
            self.tab_widget.hide()

    def _on_search(self):
        """搜索按钮点击"""
        query = self.search_input.text().strip()
        if not query:
            return

        self.progress_bar.show()
        self.search_btn.setEnabled(False)
        self._manager.search_extensions(query)

    def _on_search_completed(self, extensions: list):
        """搜索完成回调"""
        self.progress_bar.hide()
        self.search_btn.setEnabled(True)
        self._current_extensions = extensions

        # 清空现有列表（保留 search_hint，避免被销毁后再访问导致崩溃）
        self._clear_layout(self.search_layout, keep=self.search_hint)

        if not extensions:
            hint = QLabel("未找到相关扩展")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet(f"""
                QLabel {{
                    color: {self.theme.get('text_dim', '#888')};
                    font-size: 12px;
                    padding: 40px;
                }}
            """)
            self.search_layout.addWidget(hint)
        else:
            for ext in extensions:
                item = ExtensionItemWidget(ext, self.theme)
                item.install_clicked.connect(self._on_install)
                item.uninstall_clicked.connect(self._on_uninstall)
                self.search_layout.addWidget(item)

        self.search_layout.addStretch()
        self.search_hint.hide()

    def _on_install(self, extension_id: str):
        """安装扩展"""
        self.progress_bar.show()
        self._manager.install_extension(extension_id)

    def _on_uninstall(self, extension_id: str):
        """卸载扩展"""
        reply = QMessageBox.question(
            self,
            "确认卸载",
            f"确定要卸载扩展 '{extension_id}' 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.progress_bar.show()
            self._manager.uninstall_extension(extension_id)

    def _on_install_completed(self, success: bool, message: str):
        """安装/卸载完成回调"""
        self.progress_bar.hide()
        if success:
            QMessageBox.information(self, "成功", message)
            # 重新搜索以更新状态
            if self.search_input.text().strip():
                self._on_search()
        else:
            QMessageBox.warning(self, "失败", message)

    def _on_tab_changed(self, index: int):
        """Tab 切换回调"""
        if index == 1:  # 已安装 Tab
            self._manager.refresh_installed()

    def _update_installed_list(self, extensions: list):
        """更新已安装列表"""
        self._clear_layout(self.installed_layout)

        self.tab_widget.setTabText(1, f"已安装 ({len(extensions)})")

        if not extensions:
            hint = QLabel("暂无已安装的扩展")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet(f"""
                QLabel {{
                    color: {self.theme.get('text_dim', '#888')};
                    font-size: 12px;
                    padding: 40px;
                }}
            """)
            self.installed_layout.addWidget(hint)
        else:
            for ext_id in sorted(extensions):
                item = InstalledExtensionItem(ext_id, self.theme)
                item.uninstall_clicked.connect(self._on_uninstall)
                self.installed_layout.addWidget(item)

        self.installed_layout.addStretch()

    def _clear_layout(self, layout, keep=None):
        """清空布局；keep 指向的 widget 仅从布局移除、不销毁，供后续复用"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget and widget is not keep:
                widget.deleteLater()

    def _show_error(self, message: str):
        """显示错误"""
        self.progress_bar.hide()
        QMessageBox.warning(self, "错误", message)

    def apply_theme(self, theme: dict):
        """应用主题"""
        self.theme = theme
        # 重新设置样式...
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: {theme.get('bg_dark', '#1a1a2e')};
                color: {theme.get('text', '#eaeaea')};
                border: 1px solid {theme.get('border', '#3d3d5c')};
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 12px;
            }}
            QLineEdit:focus {{
                border-color: {theme.get('accent', '#667eea')};
            }}
        """)

        self.search_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {theme.get('accent', '#667eea')};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {theme.get('accent_hover', '#7a8efa')};
            }}
        """)

        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background-color: {theme.get('bg_dark', '#1a1a2e')};
            }}
            QTabBar::tab {{
                background-color: {theme.get('bg_medium', '#16213e')};
                color: {theme.get('text_dim', '#888')};
                padding: 8px 16px;
                margin-right: 2px;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }}
            QTabBar::tab:selected {{
                background-color: {theme.get('bg_dark', '#1a1a2e')};
                color: {theme.get('text', '#eaeaea')};
            }}
        """)

    def refresh(self):
        """刷新"""
        self._check_vscode()
