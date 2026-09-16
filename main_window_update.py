"""MainWindow 的「应用内更新」混入（从 main_window.py 拆出）。

自动/手动检查更新、状态栏角标、下载并确认重启安装。纯粹的方法搬迁，
行为不变；进程级去重标志 _auto_update_check_done 随类继承，写入用
type(self) 落在真正的 MainWindow 类上（多窗口共享，与拆分前一致）。

窗口恢复（restore_windows_after_update / _stash_windows_for_restore）
不在此处：它构造 MainWindow、属窗口生命周期，仍留在主类。
"""
import os
import shutil

from PyQt6.QtCore import Qt, QObject, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QMessageBox, QProgressDialog, QPushButton,
)
from PyQt6 import sip

import app_config
from app_logging import get_logger
from i18n import t


# 进程级一次性标志（_auto_update_check_done）经 window_host.host_class(self)
# 落到真正的 MainWindow 上，不 import main_window。
from window_host import host_class

logger = get_logger(__name__)


class _UpdateWorkerPool(QObject):
    """进程级：强引用运行中的更新线程（检查/下载），线程一律不设 Qt 父对象。

    以前 UpdateChecker(self) / UpdateDownloader(..., self) 以主窗口为父对象：
    主窗口是 WA_DeleteOnClose，检查还没返回就关窗 → 窗口析构连带析构运行中
    的 QThread → "QThread: Destroyed while thread is still running" → abort。
    做法与 ai_completion 的 CompletionWorker 一致：不挂父对象；由本池强引用
    防 GC（Python 侧引用归零时 sip 会当场析构 C++ QThread，同样 abort）；
    finished 后 deleteLater 并从池里移除。池活到进程结束，窗口先没了也没事，
    线程脱离窗口自行收尾。
    """

    def __init__(self):
        super().__init__()
        self._workers = set()

    def adopt(self, worker):
        self._workers.add(worker)
        worker.finished.connect(self._on_worker_finished)

    def _on_worker_finished(self):
        worker = self.sender()
        if worker is None:
            return
        self._workers.discard(worker)
        worker.deleteLater()


_worker_pool = None


def _adopt_update_worker(worker):
    """把无父对象的更新线程交给进程级池托管（见 _UpdateWorkerPool）。"""
    global _worker_pool
    if _worker_pool is None:
        _worker_pool = _UpdateWorkerPool()
    _worker_pool.adopt(worker)
    return worker


class UpdateMixin:

    def _init_update_state(self):
        """UpdateMixin 的实例状态（唯一默认值）"""
        self._update_badge = None    # 状态栏「新版本可用」角标
        self._update_checker = None  # 进行中的后台检查线程（手动检查）
        self._auto_update_checker = None  # 启动静默检查的线程
        self._update_downloader = None    # 进行中的下载线程
        self._update_progress = None      # 下载进度对话框（关窗时借它走取消路径）
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
        if host_class(self)._auto_update_check_done or sip.isdeleted(self):
            return
        cfg = app_config.read_config()
        if not cfg.get('auto_update_check', True):
            return
        if time.time() - float(cfg.get('update_last_check_ts', 0)) < 24 * 3600:
            return
        host_class(self)._auto_update_check_done = True
        app_config.update_config({'update_last_check_ts': time.time()},
                                 description='auto update check throttle')
        checker = _adopt_update_worker(app_updater.UpdateChecker())
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
        old = self._update_badge
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
        badge = self._update_badge
        if badge is not None and not sip.isdeleted(badge):
            self.statusbar.removeWidget(badge)
            badge.deleteLater()
        self._update_badge = None
        self._on_update_check_result(info, auto=True)

    def _check_for_updates(self):
        """设置菜单「检查更新」：后台查 GitHub 最新 release，不阻塞 GUI。"""
        import app_updater
        old = self._update_checker
        if old is not None and not sip.isdeleted(old) and old.isRunning():
            return   # 已在查了
        self.statusbar.showMessage(t("update.checking"), 0)
        checker = _adopt_update_worker(app_updater.UpdateChecker())
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
        # GitHub API 给的权威字节数：下载器用它校验落盘是否完整，挡住被代理
        # 截断的半截安装包（否则 Inno 会弹 "setup files are corrupted"）
        expected_size = asset.get('size') or 0
        # API 的 'sha256:<hex>'（新 release 才有）：下载器流式算哈希比对，
        # 挡住字节数对得上但内容被换掉的包；URL 本身也由下载器钉在官方仓库
        digest = asset.get('digest')
        # 下载阶段只写临时目录，中途取消没有半成品风险（真正的换包发生在
        # 下载完成、用户确认重启之后），所以取消按钮和关窗都允许中止
        progress = QProgressDialog(t("update.downloading"),
                                   t("update.cancel"), 0, 100, self)
        progress.setWindowTitle(t("update.title"))
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        # 不设 Qt 父对象：主窗口 WA_DeleteOnClose，下载中关窗会连带析构运行中
        # 的线程而 abort；交给进程级池托管，关窗只 cancel（见 _UpdateWorkerPool）
        dl = _adopt_update_worker(
            app_updater.UpdateDownloader(url, expected_size, digest=digest))
        self._update_downloader = dl
        self._update_progress = progress
        # finished: 正常收尾（on_done/on_error 关闭对话框也会触发 canceled，
        # 用它区分）；cancelled: 用户点了取消或关掉了进度窗
        state = {'finished': False, 'cancelled': False}

        def on_cancelled():
            if state['finished'] or state['cancelled']:
                return
            state['cancelled'] = True
            dl.cancel()
            if not sip.isdeleted(self):
                self.statusbar.showMessage(t("update.cancelled"), 4000)

        # 下面三个槽都可能在窗口已销毁后才收到排队信号（线程不随窗口死），
        # 先验对话框/窗口还在，别在槽里抛 RuntimeError
        def on_progress(done, total):
            if state['cancelled'] or sip.isdeleted(progress):
                return
            if total > 0:
                progress.setMaximum(100)
                progress.setValue(min(99, int(done * 100 / total)))

        def on_done(app_path):
            if state['cancelled'] or sip.isdeleted(self):
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
                # workdir 交给换包脚本：装完由脚本删（脚本本体在 /tmp 单独
                # 文件里、不在 workdir 内，末尾自删——见 app_updater 的模板）
                if app_updater.install_and_restart(app_path, workdir=dl.workdir):
                    self._close_all_windows_as_batch()
                    return
            # 用户取消重启（或本平台装不了）：下载好的包不会再用，当场清掉
            # 临时目录——否则几百 MB 的 update.zip + 解出的 .app 永远留在 /tmp
            self._discard_update_workdir(dl)

        def on_error(err):
            if state['cancelled'] or sip.isdeleted(self):
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

    def _close_all_windows_as_batch(self):
        """用户已确认「重启安装」：整批关窗，同批只弹一次"确认退出"。

        直接调 closeAllWindows() 不经过 QEvent.Quit，宿主类的 _batch_closing
        探针看不到它，这里自己置位/复位（语义同 _QuitEventFilter）。
        """
        host = host_class(self)
        host._batch_closing = True
        try:
            QApplication.instance().closeAllWindows()
        finally:
            host._batch_closing = False

    def _discard_update_workdir(self, dl):
        """删掉某次下载的临时目录（包不会再被安装时调用）。"""
        workdir = getattr(dl, 'workdir', None)
        if workdir and os.path.basename(workdir).startswith('stellar_update_'):
            shutil.rmtree(workdir, ignore_errors=True)
            dl.workdir = None

    def _shutdown_update_workers(self):
        """关窗：中止进行中的下载，不等待。

        线程没有父对象、由进程级池托管，cancel 后它在下一个读块边界自己
        收工并清理临时目录；这里绝不 wait()——那会把 GUI 线程卡到块边界。
        检查线程（几秒内必返回）同理放任其自生自灭。
        """
        try:
            progress = self._update_progress
            if progress is not None and not sip.isdeleted(progress):
                progress.cancel()   # 走 canceled → on_cancelled → dl.cancel()
        except Exception:
            logger.debug("_shutdown_update_workers: progress cancel failed",
                         exc_info=True)
        dl = self._update_downloader
        if dl is not None and not sip.isdeleted(dl) and dl.isRunning():
            dl.cancel()
