"""
主窗口的原生菜单栏（文件 / 视图 / 终端 / 窗口 / 帮助）。

从 main_window 拆出的 mixin：所有方法通过 self 落到 MainWindow。菜单只是
把已有的动作（工具栏按钮、快捷键动作）按新手能找到的方式再摆一遍，不引入
新的状态。带快捷键的项**复用** _setup_shortcuts 建好的 QAction（改文本、
加进菜单即可）——再建一个同键位的 QAction 会让 Qt 报"快捷键有歧义"，
两个都不触发。
"""
import os
import webbrowser

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QFileDialog, QMenu, QMessageBox

from i18n import t
from app_logging import get_logger
from window_host import host_class

logger = get_logger(__name__)

REPO_URL = "https://github.com/StellarStar255/stellar_smart_terminal"


class MenusMixin:
    """MainWindow 的菜单栏部分。"""

    # ---------- 搭建 ----------

    def _setup_menubar(self):
        """建整条菜单栏。语言切换后由 _rebuild_menus 重建。"""
        self._menu_owned_actions = []   # 本次建的、不属于 shortcut_actions 的 QAction
        menubar = self.menuBar()
        self._build_file_menu(menubar)
        self._build_view_menu(menubar)
        self._build_terminal_menu(menubar)
        self._build_window_menu(menubar)
        self._build_help_menu(menubar)

    def _rebuild_menus(self):
        """语言变了：清掉菜单重建。复用的快捷键动作只是改文本，不会重复注册。"""
        for act in getattr(self, '_menu_owned_actions', []):
            try:
                self.removeAction(act)
                act.setParent(None)   # 立刻脱离窗口：快捷键随之注销，不等 deleteLater
                act.deleteLater()
            except RuntimeError:
                pass
        self.menuBar().clear()
        self._setup_menubar()

    # 复用 _setup_shortcuts 建好的动作：菜单里显示同一个快捷键，且不会歧义
    def _shortcut_menu_action(self, menu, action_id, label_key):
        action = getattr(self, 'shortcut_actions', {}).get(action_id)
        if action is None:
            return None
        action.setText(t(label_key))
        menu.addAction(action)
        return action

    def _own_action(self, menu, label_key, slot, shortcut=None, checkable=False):
        action = QAction(t(label_key), self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if checkable:
            action.setCheckable(True)
        action.triggered.connect(slot)
        menu.addAction(action)
        self._menu_owned_actions.append(action)
        return action

    # ---------- 文件 ----------

    def _build_file_menu(self, menubar):
        menu = menubar.addMenu(t("menu.file"))
        self._shortcut_menu_action(menu, "new_tab", "shortcuts.act.new_tab")
        # Cmd+N / Cmd+O：终端聚焦时由 terminal_input 的 ShortcutOverride 放行
        # （macOS 上 Cmd+字母默认被终端抢走），见那里的说明
        self._own_action(menu, "menu.new_window", self._open_new_window, "Ctrl+N")
        menu.addSeparator()
        self._own_action(menu, "menu.open_folder", self._browse_working_dir, "Ctrl+O")
        self._own_action(menu, "menu.open_file", self._open_file_dialog, "Ctrl+Shift+O")
        recent = menu.addMenu(t("menu.recent_dirs"))
        recent.aboutToShow.connect(lambda m=recent: self._fill_recent_dirs_menu(m))
        menu.addSeparator()
        self._own_action(menu, "menu.export_session", self._show_export_dialog)
        self._shortcut_menu_action(menu, "history", "shortcuts.act.history")
        menu.addSeparator()
        self._shortcut_menu_action(menu, "close_tab", "shortcuts.act.close_tab")
        self._file_menu = menu

    def _fill_recent_dirs_menu(self, menu: QMenu):
        """最近用过的工作目录（按使用频率），点一下即切换。"""
        menu.clear()
        history = [p for p in getattr(self, 'working_dir_history', []) if p][:15]
        if not history:
            empty = menu.addAction(t("menu.recent_dirs_empty"))
            empty.setEnabled(False)
            return
        for path in history:
            act = menu.addAction(path)
            act.triggered.connect(lambda checked=False, p=path: self._switch_to_dir(p))

    def _switch_to_dir(self, path: str):
        """切换本窗口工作目录（等同在目录栏输入后按 Switch）。"""
        if not path:
            return
        self.working_dir_combo.setCurrentText(path)
        self._apply_working_dir()

    def _open_file_dialog(self):
        """在内置编辑器里打开一个文件。"""
        start = self._window_cwd if os.path.isdir(self._window_cwd or "") else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, t("menu.open_file_title"), start)
        if path:
            self._open_file_in_editor(path)

    def _open_new_window(self):
        """新开一个空窗口（与拆出标签的窗口同款：登记跟踪、分配颜色）。"""
        try:
            win = host_class(self)()
            try:
                win._set_window_color(self._get_available_window_color())
            except Exception:
                logger.debug("_open_new_window: color failed", exc_info=True)
            win.show()
            win.raise_()
            win.activateWindow()
            self._track_detached_window(win)
            return win
        except Exception as e:
            logger.warning(f"[Menu] open new window failed: {e}")
            return None

    # ---------- 视图 ----------

    def _build_view_menu(self, menubar):
        menu = menubar.addMenu(t("menu.view"))
        self._shortcut_menu_action(menu, "toggle_explorer", "shortcuts.act.toggle_explorer")
        self._shortcut_menu_action(menu, "toggle_git", "shortcuts.act.toggle_git")
        self._shortcut_menu_action(menu, "toggle_remote", "shortcuts.act.toggle_remote")
        self._own_action(menu, "menu.show_log", self._toggle_log_panel)
        nav = self._own_action(menu, "toolbar.window_nav",
                               self._toggle_window_navigator_from_menu, checkable=True)
        menu.aboutToShow.connect(lambda a=nav: self._sync_nav_menu_state(a))
        menu.addSeparator()
        self._shortcut_menu_action(menu, "toggle_editor", "shortcuts.act.toggle_editor")
        self._shortcut_menu_action(menu, "toggle_word_wrap", "shortcuts.act.toggle_word_wrap")
        self._shortcut_menu_action(menu, "refresh", "shortcuts.act.refresh")
        menu.addSeparator()
        self._shortcut_menu_action(menu, "zoom_in", "shortcuts.act.zoom_in")
        self._shortcut_menu_action(menu, "zoom_out", "shortcuts.act.zoom_out")
        menu.addSeparator()
        self._own_action(menu, "shortcuts.toolbar_menu_item", self._show_toolbar_manager)
        self._view_menu = menu

    def _sync_nav_menu_state(self, action):
        cb = getattr(self, 'window_nav_checkbox', None)
        if cb is not None:
            action.setChecked(cb.isChecked())

    def _toggle_window_navigator_from_menu(self):
        cb = getattr(self, 'window_nav_checkbox', None)
        if cb is not None:
            cb.setChecked(not cb.isChecked())

    # ---------- 终端 ----------

    def _build_terminal_menu(self, menubar):
        menu = menubar.addMenu(t("menu.terminal"))
        self._shortcut_menu_action(menu, "new_session", "shortcuts.act.new_session")
        self._own_action(menu, "menu.stop_session", self._stop_session)
        menu.addSeparator()
        self._shortcut_menu_action(menu, "split_h", "shortcuts.act.split_h")
        self._shortcut_menu_action(menu, "split_v", "shortcuts.act.split_v")
        self._shortcut_menu_action(menu, "close_split", "shortcuts.act.close_split")
        menu.addSeparator()
        self._own_action(menu, "menu.clear_terminal", self._clear_terminal)
        self._own_action(menu, "menu.pasted_images", self._show_pasted_images)
        menu.addSeparator()
        self._own_action(menu, "menu.open_vscode", self._open_in_vscode)
        self._own_action(menu, "menu.open_cursor", self._open_in_cursor)
        menu.addSeparator()
        self._own_action(menu, "menu.manage_presets", self._manage_presets)
        self._own_action(menu, "menu.llm_config", self._show_llm_config)
        self._terminal_menu = menu

    # ---------- 窗口 ----------

    def _build_window_menu(self, menubar):
        """原生「窗口」菜单：可点击的「下一个/上一个窗口」，并展示 Cmd+` / Cmd+Shift+`。

        说明：实际的 Cmd+` 按键处理由 _install_backtick_monitor 的 AppKit 级 keyDown
        监听器完成（见那里的说明）——菜单项这里主要用于可发现性与鼠标点击触发。
        注意菜单项的快捷键本身不会拦截系统的 Cmd+`：macOS 的「移动焦点到下一窗口」是
        系统级保留快捷键，优先级高于应用菜单/QShortcut，需用户在「系统设置 → 键盘 →
        键盘快捷键 → 键盘」中禁用后，Cmd+` 才会落到我们的监听器里稳定切换。
        """
        menu = menubar.addMenu(t("window.menu"))
        actions = getattr(self, '_window_menu_actions', None)
        if actions is None:
            # 只建一次：这两个是应用级快捷键，重建菜单时复用，不重复注册
            next_action = QAction(self)
            next_action.setShortcut(QKeySequence("Ctrl+`"))  # macOS 上 Qt 自动映射为 Cmd+`
            next_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            next_action.triggered.connect(lambda: self._cycle_to_window(1))
            prev_action = QAction(self)
            prev_action.setShortcut(QKeySequence("Ctrl+Shift+`"))  # Cmd+Shift+`
            prev_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            prev_action.triggered.connect(lambda: self._cycle_to_window(-1))
            actions = (next_action, prev_action)
            self._window_menu_actions = actions  # 防 GC
        next_action, prev_action = actions
        next_action.setText(t("window.next_window"))
        prev_action.setText(t("window.prev_window"))
        menu.addAction(next_action)
        menu.addAction(prev_action)
        menu.addSeparator()
        self._shortcut_menu_action(menu, "next_tab", "shortcuts.act.next_tab")
        self._shortcut_menu_action(menu, "prev_tab", "shortcuts.act.prev_tab")
        self._window_menu = menu

    # ---------- 帮助 ----------

    def _build_help_menu(self, menubar):
        menu = menubar.addMenu(t("menu.help"))
        self._shortcut_menu_action(menu, "cheatsheet", "shortcuts.act.cheatsheet")
        self._own_action(menu, "shortcuts.menu_item", self._show_shortcut_settings)
        menu.addSeparator()
        self._own_action(menu, "update.menu_item", self._check_for_updates)
        self._own_action(menu, "menu.github", lambda: webbrowser.open(REPO_URL))
        menu.addSeparator()
        about = self._own_action(menu, "menu.about", self._show_about_dialog)
        about.setMenuRole(QAction.MenuRole.AboutRole)   # macOS 归到应用菜单
        self._help_menu = menu

    def _show_about_dialog(self):
        try:
            from app_updater import get_current_version
            version = get_current_version() or "?"
        except Exception:
            version = "?"
        box = self._make_styled_message_box(
            QMessageBox.Icon.Information,
            t("menu.about_title"),
            t("menu.about_text", version=version, url=REPO_URL),
        )
        box.exec()
