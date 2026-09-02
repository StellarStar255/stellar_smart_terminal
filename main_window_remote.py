"""MainWindow 的远程（SSH/SFTP）侧面板混入（从 main_window.py 拆出）。

面板搭建、显示/隐藏、主机连接、SSH 终端标签/新窗口、远程文件编辑。
纯方法搬迁，行为不变；_open_ssh_in_new_window 构造新窗口/进程级窗口
计数走 _mw.MainWindow。分屏布局管道方法留在主类。
"""
import os
from PyQt6 import sip
from PyQt6.QtCore import QTimer, Qt
from i18n import t
from app_logging import get_logger
# RemoteExplorerPanel 在 _ensure_remote_panel 里按需 import：它的 import 链
# 拉进 ssh_control → ssh_session → paramiko/cryptography，占启动 import 时间
# 近三成，而远程面板默认隐藏、多数会话根本不打开。
# 延迟引用宿主类：进程级共享类属性/构造新窗口须落在真 MainWindow，
# 只在方法内访问，循环 import 安全。
import main_window as _mw

logger = get_logger(__name__)


class RemotePanelMixin:

    def _setup_remote_panel(self):
        """设置 Remote Explorer 面板（SSH/SFTP）"""
        from PyQt6.QtWidgets import (
            QVBoxLayout, QHBoxLayout, QFrame, QLabel, QPushButton, QCheckBox, QSplitter
        )

        layout = QVBoxLayout(self.remote_panel_container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        self._remote_header = QFrame()
        self._remote_header.setStyleSheet("""
            QFrame { background-color: #16213e; border-bottom: 1px solid #3d3d5c; }
        """)
        rh_layout = QHBoxLayout(self._remote_header)
        rh_layout.setContentsMargins(10, 5, 10, 5)

        self._remote_title = QLabel(t("remote.title"))
        self._remote_title.setStyleSheet("color: #38bdf8; font-weight: bold;")
        rh_layout.addWidget(self._remote_title)
        rh_layout.addStretch()

        # 分屏方向切换 checkbox（与 Explorer 行为一致：勾选=左右分屏，不勾=上下分屏）
        self._remote_split_checkbox = QCheckBox(t("explorer.left_right_split"))
        self._remote_split_checkbox.setToolTip(t("explorer.split_tooltip"))
        self._remote_split_checkbox.setStyleSheet("""
            QCheckBox { color: #888; font-size: 11px; spacing: 4px; }
            QCheckBox:hover { color: #eaeaea; }
            QCheckBox::indicator { width: 14px; height: 14px; }
            QCheckBox::indicator:unchecked {
                border: 1px solid #3d3d5c; border-radius: 2px; background-color: #16213e;
            }
            QCheckBox::indicator:checked {
                border: 1px solid #667eea; border-radius: 2px; background-color: #667eea;
            }
        """)
        if not hasattr(self, '_remote_split_horizontal'):
            self._remote_split_horizontal = False  # 默认上下分屏
        self._remote_split_checkbox.stateChanged.connect(self._on_remote_split_orientation_changed)
        rh_layout.addWidget(self._remote_split_checkbox)

        # 弹簧模式 checkbox（与 Explorer 共用同一个全局开关，两处自动同步）
        self._remote_spring_checkbox = QCheckBox(t("explorer.spring_mode"))
        self._remote_spring_checkbox.setToolTip(t("explorer.spring_tooltip"))
        self._remote_spring_checkbox.setStyleSheet(self._remote_split_checkbox.styleSheet())
        self._remote_spring_checkbox.setChecked(bool(getattr(self, '_spring_mode_enabled', False)))
        self._remote_spring_checkbox.stateChanged.connect(self._on_spring_mode_toggled)
        rh_layout.addWidget(self._remote_spring_checkbox)

        hide_btn = QPushButton("×")
        hide_btn.setFixedSize(24, 24)
        hide_btn.setStyleSheet("""
            QPushButton { background-color: transparent; color: #888; border: none; font-size: 16px; }
            QPushButton:hover { color: #eaeaea; }
        """)
        hide_btn.clicked.connect(self._toggle_remote_panel)
        rh_layout.addWidget(hide_btn)

        layout.addWidget(self._remote_header)

        # RemoteExplorerPanel 本体推迟到首次打开再建（见 _ensure_remote_panel）

        # 用竖直 QSplitter 包住远程树，以便上下分屏时把编辑器放到树下方
        # （编辑器 editor_area 为共享单例，按需在 remote_splitter / main_splitter 间移动）
        self.remote_splitter = QSplitter(Qt.Orientation.Vertical)
        self.remote_splitter.setHandleWidth(3)
        self.remote_splitter.setStyleSheet("""
            QSplitter::handle { background-color: #3d3d5c; }
            QSplitter::handle:hover { background-color: #667eea; }
        """)
        self.remote_splitter.setSizes([400, 0])
        self.remote_splitter.splitterMoved.connect(lambda *_: self._capture_remote_layout())
        layout.addWidget(self.remote_splitter)

    def _ensure_remote_panel(self):
        """首次需要时才构造 RemoteExplorerPanel 并接线；之后直接返回已建实例。"""
        panel = getattr(self, 'remote_panel', None)
        if panel is not None:
            return panel
        from remote_explorer_widget import RemoteExplorerPanel
        current_theme = self.THEMES.get(self.current_theme, self.THEMES["午夜黑"])
        panel = RemoteExplorerPanel(theme=current_theme)
        self.remote_panel = panel
        self.remote_splitter.insertWidget(0, panel)
        if not (hasattr(self, 'editor_area') and self.editor_area.isVisible()):
            self.remote_splitter.setSizes([400, 0])
        # 远程文件打开 → 注入到本地编辑器（透明处理远程保存）
        panel.file_open_requested.connect(self._open_remote_file_in_editor)
        # 连接成功后自动开一个 SSH 终端 tab
        panel.host_connected.connect(self._on_remote_host_connected)
        # 右键 "在此处打开终端" → 同样开一个 SSH tab，且 cd 进指定目录
        panel.open_terminal_at.connect(self._open_ssh_terminal_tab)
        # 右键 "在新窗口中连接" → 新开一个独立窗口并 SSH 进该主机
        panel.open_in_new_window.connect(self._open_ssh_in_new_window)
        return panel

    def _toggle_remote_panel(self):
        """切换 Remote Explorer 面板显示（与 Explorer / Git 互斥）"""
        self.remote_panel_visible = not getattr(self, 'remote_panel_visible', False)
        if hasattr(self, 'remote_toggle_btn'):
            self.remote_toggle_btn.setChecked(self.remote_panel_visible)

        self.main_splitter.setUpdatesEnabled(False)

        if self.remote_panel_visible:
            # 隐藏 Explorer / Git
            if self.explorer_panel_visible:
                self.explorer_panel_visible = False
                self.explorer_toggle_btn.setChecked(False)
                self.explorer_panel_container.hide()
            if self.git_panel_visible:
                self.git_panel_visible = False
                self.git_toggle_btn.setChecked(False)
                self.git_panel_container.hide()
                self._hide_git_diff()  # 切到 Remote 时若在看 diff，回到终端

            # 先把编辑器收回默认家（避免它停在 explorer/main_splitter 里）
            self._home_editor_hidden()

            self._ensure_remote_panel()
            self.remote_panel_container.show()
            self.left_panel_container.show()

            # 若之前有打开的文件，按 Side-by-Side 偏好恢复编辑器位置（与 Explorer 一致）
            if hasattr(self, 'editor_area') and self._editor_has_any_file():
                if self._remote_split_horizontal:
                    self._place_editor_in_main_splitter()
                else:
                    self._place_editor_in_remote_splitter()
        else:
            # 有打开文件（含远程文件）时编辑器不消失——在 hide 容器前判断,
            # 内嵌在 remote_splitter 里的迁到 main_splitter 继续显示
            keep_editor = (hasattr(self, 'editor_area')
                           and self.editor_area.isVisible()
                           and self._editor_has_any_file())
            self.remote_panel_container.hide()
            if keep_editor:
                if self.main_splitter.indexOf(self.editor_area) < 0:
                    self._place_editor_in_main_splitter()
            else:
                # 没有打开文件：收回默认家，避免遗留在隐藏的 remote_splitter 内
                self._home_editor_hidden()
            if not self.explorer_panel_visible and not self.git_panel_visible:
                self.left_panel_container.hide()

        self._sync_embedded_nav()
        self._update_splitter_sizes()
        self.main_splitter.setUpdatesEnabled(True)
        self._reconcile_spring_after_layout_change()
        QTimer.singleShot(0, self._flush_terminal_resizes)

    def _on_remote_host_connected(self, host_config):
        """Remote 面板连上主机 → 默认自动开一个 SSH 终端 tab。

        但「扩展远程终端到新窗口」时，被扩展的终端已经在新窗口里了，会先置
        _skip_auto_ssh_tab_once，让这一次只连 Remote 文件树、不再多开一个终端。
        """
        if getattr(self, '_skip_auto_ssh_tab_once', False):
            self._skip_auto_ssh_tab_once = False
            return
        self._open_ssh_terminal_tab(host_config, None)

    def _auto_connect_remote_in_window(self, new_window, host_config):
        """让新窗口的 Remote 面板自动连到 host_config（扩展远程终端到新窗口时用）。

        抑制一次「连上后自动开 SSH 终端」——被扩展的终端已经在新窗口里。
        延迟到窗口初始化稳定后再连。"""
        # 把原窗口已缓存的密码带给新窗口，避免自动连 SFTP 时再弹一次密码框
        try:
            pw = self._ensure_remote_panel().get_cached_password(host_config.alias)
            new_window._ensure_remote_panel().prime_cached_password(host_config.alias, pw)
        except Exception:
            logger.debug("_auto_connect_remote_in_window: suppressed exception", exc_info=True)

        def _go():
            if sip.isdeleted(new_window):
                return
            try:
                if not getattr(new_window, 'remote_panel_visible', False):
                    new_window._toggle_remote_panel()
                new_window._skip_auto_ssh_tab_once = True
                new_window.remote_panel._connect_to(host_config)
            except Exception as e:
                try:
                    new_window.statusbar.showMessage(
                        f"Remote connect failed: {e}", 5000)
                except Exception:
                    logger.debug("_go: suppressed exception", exc_info=True)
        QTimer.singleShot(120, _go)

    def _open_ssh_terminal_tab(self, host_config, remote_cd_path):
        """新开一个 tab 跑 ssh 到远端

        Args:
            host_config: ssh_session.HostConfig（用别名/host/user/port/key/proxyjump）
            remote_cd_path: 可选，连接后在远程 cd 到该目录；为 None 则去 $HOME
        """
        # 构造 ssh 命令：统一走 build_ssh_terminal_command（含 claude 相关
        # 环境注入——export 后 exec 登录 shell，远程 claude 与本地体验一致）
        from ssh_session import build_ssh_terminal_command, bastion_boot_line
        alias = host_config.alias
        # MFA 主机基本都是 JumpServer 式网关：它们不接受"远程命令"，递过去
        # 只会一片黑。这类主机只发 `ssh -tt <目标>`，让网关自己出 shell/菜单，
        # 环境注入与 cd 等 shell 出来后当用户输入补发。
        bastion = False
        try:
            bastion = bool(self._ensure_remote_panel()._is_mfa_host(alias))
        except Exception:
            logger.debug("_open_ssh_terminal_tab: mfa host probe failed",
                         exc_info=True)
        cmd_string = build_ssh_terminal_command(host_config, remote_cd_path,
                                                bastion=bastion)

        # 标签名标记 SSH host
        tab_name = t("remote.terminal_tab_name", host=alias)
        # 若当前 tab 只有一个、且 shell 还没真正启动的空白终端，直接复用它，
        # 避免每次远程连接都新建一个 tab 造成浪费；否则新开一个 tab。
        cur_idx = self.tab_widget.currentIndex()
        cur_terms = self.tab_terminals.get(cur_idx, [])
        if cur_idx >= 0 and len(cur_terms) == 1 and not cur_terms[0].has_started():
            idx = cur_idx
            _page = self.tab_widget.widget(idx)
            if not getattr(_page, '_custom_tab_name', None):
                self.tab_widget.setTabText(idx, tab_name)
        else:
            idx = self._add_new_tab(tab_name=tab_name)
        # 获取这个 tab 的第一个终端，启动 ssh
        terms = self.tab_terminals.get(idx, [])
        if not terms:
            return
        term = terms[0]
        # 记下这个终端连的是哪台远端，供「扩展为新窗口」时让新窗口的 Remote
        # 面板自动连到同一主机（比解析标题可靠——标题可被「重命名标签」改掉）。
        term._ssh_host_config = host_config
        self.tab_widget.setCurrentIndex(idx)
        self.active_terminal = term
        term.setFocus()
        # 复用当前 tab 时 currentChanged 不会触发，显式同步窗口标题 + 导航面板，
        # 确保 Navigator 立即显示这台新连上的远程主机。
        self._update_window_title_from_tab(idx)
        # 若用户刚在 Remote 面板里为该主机输入过密码，预置一次性自动回填，
        # 这样终端里的 ssh 密码提示就不用再输一遍。
        try:
            cached_pw = self._ensure_remote_panel().get_cached_password(alias)
            if cached_pw:
                term.arm_password_autofill(cached_pw)
        except Exception:
            logger.debug("_open_ssh_terminal_tab: suppressed exception", exc_info=True)
        # 用 _start_and_execute：先起 shell，再回车跑 ssh；ssh 退出后用户回到本地 shell
        try:
            term._start_and_execute([cmd_string])
            # 标签页徽章：SSH 会话已启动 → 运行状态点
            self._refresh_tab_badge(idx)
            if bastion:
                self._send_bastion_boot_line(term, bastion_boot_line(remote_cd_path))
        except Exception as e:
            self.statusbar.showMessage(f"Failed to start SSH: {e}", 5000)

    def _send_bastion_boot_line(self, term, line: str, settle_ms: int = 1200):
        """堡垒机登录后，把环境注入 / cd 当作用户输入补发进去。

        远程命令递不进这类网关（递了就一片黑），只能等它把 shell 吐出来再敲。
        触发点是「远端来了第一段输出」而不是固定延时——堡垒机登录快慢差很多，
        定时器要么敲早了打进认证过程，要么白等。收到首段输出后再让子弹飞
        settle_ms，等登录横幅/菜单打完，然后敲一行。

        远端一直没输出就什么都不发（宁可不 cd，也别往未知状态里乱敲）。
        """
        if not line:
            return
        state = {"done": False}

        def _send():
            if state["done"]:
                return
            state["done"] = True
            try:
                if sip.isdeleted(term):
                    return
                term.send_text(line + "\n")
            except Exception:
                logger.debug("_send_bastion_boot_line: send failed", exc_info=True)

        def _on_output(_data):
            if state["done"]:
                return
            try:
                term.raw_output_received.disconnect(_on_output)
            except Exception:
                logger.debug("_send_bastion_boot_line: disconnect failed",
                             exc_info=True)
            QTimer.singleShot(settle_ms, _send)

        try:
            term.raw_output_received.connect(_on_output)
        except Exception:
            logger.debug("_send_bastion_boot_line: connect failed", exc_info=True)

    def _open_ssh_in_new_window(self, host_config):
        """新开一个独立窗口，并在其中完整连接该主机（Remote 右键「在新窗口中连接」）。

        走和普通 Connect 完全一样的链路：调用新窗口 Remote 面板的 _connect_to()，
        面板连上后会自动通过 host_connected 在新窗口里开 SSH 终端 tab。这样新窗口的
        Remote Explorer（SFTP 文件浏览）和终端都是连着的，而不是只有终端。
        """
        alias = getattr(host_config, 'alias', '') or 'SSH'
        _mw.MainWindow._window_counter += 1
        window_title = (f"{t('remote.terminal_tab_name', host=alias)} "
                        f"- Smart Terminal #{_mw.MainWindow._window_counter}")
        # 不传 initial_tab_data → 新窗口自建一个空白（未启动）tab，待连上后复用它跑 ssh
        new_window = _mw.MainWindow(window_title=window_title)

        # 自动配色，方便和其它窗口区分
        try:
            new_window._set_window_color(self._get_available_window_color())
        except Exception:
            logger.debug("_open_ssh_in_new_window: suppressed exception", exc_info=True)

        # 把父窗口已缓存的密码带给新窗口（和「expand to new window」一致）。
        # 否则新窗口 _connect_to 会弹模态密码框——而新窗口此刻正与父窗口逐像素
        # 重合、且处于隐形对齐期，密码框会被盖住/吞掉，用户永远没法输入，后台
        # SSH 线程就一直卡在「Connecting…」。提前预置密码即可免去这次弹框。
        try:
            pw = self._ensure_remote_panel().get_cached_password(alias)
            new_window._ensure_remote_panel().prime_cached_password(alias, pw)
        except Exception:
            # 预置失败不致命（新窗口会自行弹密码框），但要记日志：
            # 曾因静默吞异常导致「SSH 密码框死锁」难以定位
            logger.exception("failed to prime cached SSH password for %s", alias)

        # 与「expand to new window」一致：新窗口直接与父窗口逐像素重合
        # （先继承父窗口尺寸，再隐形对齐后显形，见 _align_child_with_parent_geometry），
        # 而不是简单偏移 48px——后者会被 macOS 级联/约束推走、跟父窗口对不齐。
        try:
            new_window.resize(self.size())
        except Exception:
            logger.debug("_open_ssh_in_new_window: suppressed exception", exc_info=True)
        self._align_child_with_parent_geometry(new_window)
        new_window.raise_()
        new_window.activateWindow()
        self._track_detached_window(new_window)

        # 等窗口初始化 / 首次显示稳定后再连接
        def _connect_after_init():
            if sip.isdeleted(new_window):
                return
            try:
                # 父窗口未连接时这里会弹模态密码/passphrase 框。必须保证此刻新窗口
                # 已经显形（不在隐形对齐期）、且在最上层，否则密码框会被压在窗口下
                # 看不见，用户无法输入，后台 SSH 线程就一直卡在「Connecting…」。
                # 这里在连接前强制把窗口显形并置顶，不依赖对齐循环的显形时机。
                try:
                    op = getattr(new_window, '_window_opacity', 100)
                    if not (isinstance(op, int) and 10 <= op <= 100):
                        op = 100
                    new_window.setWindowOpacity(op / 100.0)
                    new_window.raise_()
                    new_window.activateWindow()
                except Exception:
                    logger.debug("_connect_after_init: suppressed exception", exc_info=True)
                # 显示新窗口的 Remote 面板，让用户看到连上的文件树
                if not getattr(new_window, 'remote_panel_visible', False):
                    new_window._toggle_remote_panel()
                # 完整连接：连上 Remote Explorer，并经 host_connected 自动开 SSH 终端
                new_window.remote_panel._connect_to(host_config)
            except Exception as e:
                new_window.statusbar.showMessage(f"Failed to connect: {e}", 5000)
        # 稍微延后：让对齐循环前期密集的 setGeometry（~前 8 个 30ms tick）基本
        # 收敛、窗口稳定下来后再连接，避免密码框弹出时还在被反复挪动/压窄。
        QTimer.singleShot(280, _connect_after_init)

    def _open_remote_file_in_editor(self, host_alias: str, remote_path: str,
                                      local_temp_path: str, session):
        """远程 Explorer 双击文件后由本方法打开编辑器，并把保存事件转换成上传"""
        if not hasattr(self, 'editor_area'):
            return
        ok = self.editor_area.open_file_in_active(local_temp_path)
        if not ok:
            return
        # 在打开该远程文件的那个窗格上挂保存->上传逻辑（精确到窗格，避免活动窗格切换后错挂）
        pane = self.editor_area.active_pane
        # 把编辑器的「已保存」信号转成上传调用（only this file）
        # 用一个一次性的连接，文件切换时自动清理
        if not hasattr(self, '_remote_save_connections'):
            self._remote_save_connections = {}
        # 断开旧的连接（如果有）
        old = self._remote_save_connections.pop(local_temp_path, None)
        if old:
            try:
                pane.file_saved.disconnect(old)
            except Exception:
                logger.debug("_open_remote_file_in_editor: suppressed exception", exc_info=True)
        def on_saved(saved_path: str):
            if saved_path != local_temp_path:
                return
            # 把本地临时文件 push 回远端
            self._ensure_remote_panel().upload_after_save(local_temp_path)
        pane.file_saved.connect(on_saved)
        self._remote_save_connections[local_temp_path] = on_saved

        # 让编辑器标题显示远程身份（在 file_label 后追加）
        try:
            current = pane.file_label.text()
            pane.file_label.setText(
                f"{current}  ·  {t('remote.editing_remote', host=host_alias, path=remote_path)}"
            )
        except Exception:
            logger.debug("_open_remote_file_in_editor: suppressed exception", exc_info=True)

        # 按 Side-by-Side 开关放置编辑器，行为与本地 Explorer 一致：
        # 勾选=左右分屏（编辑器进 main_splitter，紧邻 Remote 树）；
        # 不勾=上下分屏（编辑器进 remote_splitter，落在 Remote 树下方，
        # 中间有可拖拽的分隔条调整两者高度）。
        # 编辑器若已显示在正确的 splitter 里，打开新文件不再重新放置，
        # 避免扰动其它编辑窗格 / 分屏的尺寸。
        target = self.main_splitter if self._remote_split_horizontal else self.remote_splitter
        if not self._editor_placed_and_visible(target):
            if self._remote_split_horizontal:
                self._place_editor_in_main_splitter()
            else:
                self._place_editor_in_remote_splitter()

        # 弹簧模式下打开远程文件：自动把编辑器展宽（与本地 Explorer 一致）
        if self._spring_applicable():
            self._apply_spring('editor')
