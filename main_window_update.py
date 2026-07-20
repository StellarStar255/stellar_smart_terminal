"""MainWindow 的「应用内更新」混入（从 main_window.py 拆出）。

自动/手动检查更新、状态栏角标、下载并确认重启安装。纯粹的方法搬迁，
行为不变；进程级去重标志 _auto_update_check_done 随类继承，写入用
type(self) 落在真正的 MainWindow 类上（多窗口共享，与拆分前一致）。

窗口恢复（restore_windows_after_update / _stash_windows_for_restore）
不在此处：它构造 MainWindow、属窗口生命周期，仍留在主类。
"""
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QMessageBox, QProgressDialog, QPushButton,
)
from PyQt6 import sip

import app_config
from i18n import t


class UpdateMixin:
    """应用内更新相关方法。依赖宿主类提供 self.statusbar、
    self._styled_message_box / _make_styled_message_box、
    self._stash_windows_for_restore。"""

    # 进程内只自动检查一次（多窗口时由最先到点的窗口执行）
    _auto_update_check_done = False

    def _maybe_auto_check_updates(self):
        """启动后的静默更新检查：每日最多一次、可在设置 ⚙ 里关闭。

        发现新版只在状态栏挂一个可点击的角标，不弹窗打扰；用户对某个
        版本点过「取消」就不再自动提醒该版本（手动检查不受影响）。
        检查失败完全静默——启动期不该为此打扰用户。
        """
        import time
        import app_updater
        if type(self)._auto_update_check_done or sip.isdeleted(self):
            return
        cfg = app_config.read_config()
        if not cfg.get('auto_update_check', True):
            return
        if time.time() - float(cfg.get('update_last_check_ts', 0)) < 24 * 3600:
            return
        type(self)._auto_update_check_done = True
        app_config.update_config({'update_last_check_ts': time.time()},
                                 description='auto update check throttle')
        checker = app_updater.UpdateChecker(self)
        self._auto_update_checker = checker
        checker.result.connect(self._on_auto_update_result)
        checker.error.connect(lambda _e: None)
        checker.start()

    def _on_auto_update_result(self, info: dict):
        import app_updater
        cur_v = app_updater.parse_version(app_updater.get_current_version())
        tag = info.get('tag', '')
        latest_v = app_updater.parse_version(tag)
        if not latest_v or (cur_v and latest_v <= cur_v):
            return
        if tag == app_config.read_config().get('update_skipped_tag'):
            return
        self._show_update_badge(info)

    def _show_update_badge(self, info: dict):
        """状态栏右侧挂「⬆ 新版本可用」角标，点击进入现有更新弹窗流程。"""
        old = getattr(self, '_update_badge', None)
        if old is not None and not sip.isdeleted(old):
            self.statusbar.removeWidget(old)
            old.deleteLater()
        badge = QPushButton(t("update.badge", version=info.get('tag', '')))
        badge.setCursor(Qt.CursorShape.PointingHandCursor)
        badge.setStyleSheet("""
            QPushButton {
                background: transparent; border: none;
                color: #667eea; padding: 0 8px;
                text-decoration: underline;
            }
            QPushButton:hover { color: #7a8efa; }
        """)
        badge.clicked.connect(lambda: self._on_update_badge_clicked(info))
        self.statusbar.addPermanentWidget(badge)
        self._update_badge = badge

    def _on_update_badge_clicked(self, info: dict):
        badge = getattr(self, '_update_badge', None)
        if badge is not None and not sip.isdeleted(badge):
            self.statusbar.removeWidget(badge)
            badge.deleteLater()
        self._update_badge = None
        self._on_update_check_result(info, auto=True)

    def _check_for_updates(self):
        """设置菜单「检查更新」：后台查 GitHub 最新 release，不阻塞 GUI。"""
        import app_updater
        if getattr(self, '_update_checker', None) is not None \
                and self._update_checker.isRunning():
            return   # 已在查了
        self.statusbar.showMessage(t("update.checking"), 0)
        checker = app_updater.UpdateChecker(self)
        self._update_checker = checker
        checker.result.connect(self._on_update_check_result)
        checker.error.connect(self._on_update_check_error)
        checker.start()

    def _on_update_check_error(self, err: str):
        self.statusbar.clearMessage()
        self._styled_message_box(
            QMessageBox.Icon.Warning, t("update.title"),
            t("update.check_failed", error=err))

    def _on_update_check_result(self, info: dict, auto: bool = False):
        """展示更新弹窗。auto=True 表示来自启动角标：用户取消时记住该版本，
        自动提醒不再骚扰（手动检查仍会正常弹出）。"""
        import app_updater
        self.statusbar.clearMessage()
        cur = app_updater.get_current_version()
        cur_v = app_updater.parse_version(cur)
        latest_tag = info.get('tag', '')
        latest_v = app_updater.parse_version(latest_tag)
        if cur_v and latest_v and latest_v <= cur_v:
            self._styled_message_box(
                QMessageBox.Icon.Information, t("update.title"),
                t("update.up_to_date", version=cur))
            return

        notes = (info.get('notes') or '').strip()
        if len(notes) > 1200:
            notes = notes[:1200] + '…'
        box = QMessageBox(self)
        box.setWindowTitle(t("update.title"))
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(t("update.available", latest=latest_tag,
                      current=cur or '?'))
        if notes:
            box.setDetailedText(notes)
        # 打包版（mac/Windows）且该 release 带对应平台产物才提供一键安装
        can_install = (app_updater.can_self_update()
                       and info.get('asset') is not None)
        install_btn = None
        if can_install:
            install_btn = box.addButton(
                t("update.download_install"), QMessageBox.ButtonRole.AcceptRole)
        page_btn = box.addButton(
            t("update.open_page"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is page_btn:
            QDesktopServices.openUrl(QUrl(app_updater.RELEASES_PAGE))
        elif install_btn is not None and clicked is install_btn:
            self._start_update_download(info['asset'])
        elif auto:
            # 自动提醒被取消：这个版本别再弹角标（出更新的版本会重新提醒）
            app_config.update_config({'update_skipped_tag': latest_tag},
                                     description='skip update tag')

    def _start_update_download(self, asset: dict):
        """下载更新 zip（带进度），完成后确认重启安装。"""
        import app_updater
        url = asset.get('browser_download_url')
        if not url:
            return
        # 下载阶段只写临时目录，中途取消没有半成品风险（真正的换包发生在
        # 下载完成、用户确认重启之后），所以取消按钮和关窗都允许中止
        progress = QProgressDialog(t("update.downloading"),
                                   t("update.cancel"), 0, 100, self)
        progress.setWindowTitle(t("update.title"))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        dl = app_updater.UpdateDownloader(url, self)
        self._update_downloader = dl
        # finished: 正常收尾（on_done/on_error 关闭对话框也会触发 canceled，
        # 用它区分）；cancelled: 用户点了取消或关掉了进度窗
        state = {'finished': False, 'cancelled': False}

        def on_cancelled():
            if state['finished'] or state['cancelled']:
                return
            state['cancelled'] = True
            dl.cancel()
            self.statusbar.showMessage(t("update.cancelled"), 4000)

        def on_progress(done, total):
            if state['cancelled']:
                return
            if total > 0:
                progress.setMaximum(100)
                progress.setValue(min(99, int(done * 100 / total)))

        def on_done(app_path):
            if state['cancelled']:
                return   # 取消后才送达的完成信号：不再弹重启确认
            state['finished'] = True
            progress.close()
            box = self._make_styled_message_box(
                QMessageBox.Icon.Question, t("update.title"),
                t("update.restart_confirm"),
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            reopen_chk = QCheckBox(t("update.reopen_windows"), box)
            reopen_chk.setChecked(bool(app_config.read_config().get(
                'update_reopen_windows', True)))
            box.setCheckBox(reopen_chk)
            if box.exec() == QMessageBox.StandardButton.Ok:
                reopen = reopen_chk.isChecked()
                app_config.update_config({'update_reopen_windows': reopen},
                                         description='update reopen pref')
                if reopen:
                    self._stash_windows_for_restore()
                if app_updater.install_and_restart(app_path):
                    QApplication.instance().closeAllWindows()

        def on_error(err):
            if state['cancelled']:
                return
            state['finished'] = True
            progress.close()
            self._styled_message_box(
                QMessageBox.Icon.Warning, t("update.title"),
                t("update.download_failed", error=err))

        # 取消按钮和标题栏关闭都会发 canceled（Qt 在 closeEvent 里同样发射）
        progress.canceled.connect(on_cancelled)
        dl.progress.connect(on_progress)
        dl.finished_ok.connect(on_done)
        dl.error.connect(on_error)
        dl.start()
