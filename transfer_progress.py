# -*- coding: utf-8 -*-
"""批量传输的统一进度窗口：一次粘贴/上传只开一个窗口，每个条目占一行。

以前一次粘贴里每个条目（甚至每个 stat / download / upload 阶段）都弹一个
QProgressDialog —— 粘 25 个文件就是几十次弹框闪烁，既看不出整体进度，
出错了也说不清是哪一项。这里把一整批收敛成一个窗口：

    顶部：总说明 + 当前阶段（长路径中段省略，不会撑宽窗口）
    中间：总进度条（按「已完成条目 + 当前条目的完成比例」推进）
    下面：每个条目一行 —— 等待 / 进行中(带速率) / 已完成 / 失败(带原因)

窗口非模态：大批量传输期间应用照常可用。取消按钮只置位 + 发信号，
真正的中断动作（abort SSH 会话等）由调用方接信号处理。
"""
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QSizePolicy, QAbstractItemView,
    QApplication,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QPainter, QFontMetrics, QColor, QPalette

from i18n import t

# 进度条刻度（千分比）：条目数少时也能按字节平滑推进
BAR_SCALE = 1000

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_SKIPPED = "skipped"

_ROLE_ROW = Qt.ItemDataRole.UserRole


class ElidedLabel(QLabel):
    """中段省略的单行标签：长远端路径不会把窗口撑到屏幕外，也不会换行抖动。"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full = text or ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)

    def setText(self, text: str):          # noqa: N802 — 覆盖 Qt 接口
        self._full = text or ""
        self.setToolTip(self._full)
        super().setText(self._full)
        self.update()

    def paintEvent(self, event):           # noqa: N802 — Qt 回调
        painter = QPainter(self)
        metrics = QFontMetrics(self.font())
        elided = metrics.elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                    max(0, self.width()))
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.drawText(self.rect(),
                         int(self.alignment()) | int(Qt.TextFlag.TextSingleLine),
                         elided)


class TransferProgressDialog(QDialog):
    """一批传输一个窗口。调用方按条目下标推进状态。

    典型用法（调用方在阻塞等待里反复调用）：
        dlg = TransferProgressDialog(names, parent=panel, header="正在粘贴到 …")
        dlg.canceled.connect(abort_everything)
        dlg.set_active_rows([0]); dlg.set_stage("正在下载 …")
        dlg.set_stage_progress(0.4, "4 MB / 10 MB · 1.2 MB/s")
        dlg.finish_row(0)                      # 或 finish_row(0, error="...")
        dlg.finish_all()                       # 全绿则自动关闭，有失败则留窗
    """

    canceled = pyqtSignal()
    # 用户手动收起 / 重新打开窗口（True = 现在可见）。调用方据此在面板上
    # 显示「传输进度」按钮，收起后还能找回来。
    visibility_changed = pyqtSignal(bool)

    def __init__(self, rows, parent=None, title: str = "", header: str = "",
                 delay_ms: int = 300):
        super().__init__(parent)
        self._base_title = title or t("transfer.title")
        self.setWindowTitle(self._base_title)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        # 传输窗口要一直看得见：非模态窗口很容易被别的窗口盖住再也找不到。
        # 嫌挡事就点「隐藏」，面板上的按钮能随时把它叫回来。
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.finished.connect(self.deleteLater)
        self._user_hidden = False
        self._auto_hidden = False   # 因模态框临时藏起来（非用户操作）

        self._names = list(rows)
        self._states = [STATE_PENDING] * len(self._names)
        self._errors: dict = {}
        self._active: list = []
        self._frac = 0.0
        self._canceled = False
        self._finished = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self._header_text = header or ""
        self._stage_base = ""      # 当前阶段说明
        self._aggregate = ""       # 整批的字节/速率（多行同时在传时放这儿）
        self._header = ElidedLabel(self._header_text)
        layout.addWidget(self._header)

        self._stage = ElidedLabel("")
        stage_font = self._stage.font()
        stage_font.setPointSizeF(max(8.0, stage_font.pointSizeF() - 1))
        self._stage.setFont(stage_font)
        layout.addWidget(self._stage)

        self._bar = QProgressBar()
        self._bar.setRange(0, BAR_SCALE)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        layout.addWidget(self._bar)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels([t("transfer.col_name"),
                                    t("transfer.col_status")])
        self._tree.setRootIsDecorated(False)
        # 长文件名多半只有中段不同（时间戳/哈希尾缀）——从中间省略才认得出
        self._tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header_view = self._tree.header()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self._tree.setColumnWidth(1, 220)
        for name in self._names:
            item = QTreeWidgetItem([name, t("transfer.state_pending")])
            item.setToolTip(0, name)
            self._tree.addTopLevelItem(item)
        layout.addWidget(self._tree, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        # 「隐藏」只是收起窗口，传输照常继续；真正中断走「取消」
        self._hide_button = QPushButton(t("transfer.hide"))
        self._hide_button.clicked.connect(self.hide_for_now)
        buttons.addWidget(self._hide_button)
        self._button = QPushButton(t("transfer.cancel"))
        self._button.clicked.connect(self._on_button)
        buttons.addWidget(self._button)
        layout.addLayout(buttons)

        self.resize(620, min(520, 200 + 22 * min(len(self._names), 12)))
        self._sync_summary()
        # 有模态框弹出来时自动让位（详见 _sync_with_modals）
        self._modal_watch = QTimer(self)
        self._modal_watch.setInterval(200)
        self._modal_watch.timeout.connect(self._sync_with_modals)
        self._modal_watch.start()
        # 秒完成的批次不该闪一下窗口：延时显示，期间完成就不再露面
        if delay_ms > 0:
            QTimer.singleShot(delay_ms, self._show_if_running)
        else:
            self._show_if_running()

    # ---------- 状态推进 ----------

    def set_header(self, text: str):
        self._header_text = text or ""
        self._header.setText(self._header_text)
        self._refresh_stage()

    def set_stage(self, text: str):
        """当前阶段说明（"正在下载 /a/b…"）。"""
        self._stage_base = text or ""
        self._refresh_stage()

    def set_active_rows(self, indices, detail: str = ""):
        """把这些条目标记为进行中，并把「当前批完成比例」清零。"""
        self._active = [i for i in indices if 0 <= i < len(self._states)]
        self._frac = 0.0
        for i in self._active:
            if self._states[i] == STATE_PENDING:
                self._states[i] = STATE_RUNNING
            self._set_status_text(i, detail or t("transfer.state_running"))
        self._sync_summary()

    def requeue_row(self, index: int):
        """把一行退回「等待中」。

        本地文件是攒成一批最后统一传的：扫到它时为它做了准备工作（解冲突、
        覆盖前删旧文件）所以标了「进行中」，但准备完就是排队等着，并没有在
        传数据——继续显示「进行中」是骗人的。
        """
        if not (0 <= index < len(self._states)):
            return
        if self._states[index] != STATE_RUNNING:
            return
        self._states[index] = STATE_PENDING
        self._set_status_text(index, t("transfer.state_pending"))
        self._active = [i for i in self._active if i != index]
        self._frac = 0.0
        self._sync_summary()

    def set_row_detail(self, index: int, text: str):
        """给某一行单独写状态文案（只对进行中的行生效）。"""
        if 0 <= index < len(self._states) and self._states[index] == STATE_RUNNING:
            self._set_status_text(index, text)

    def set_stage_progress(self, fraction: float, detail: str = ""):
        """当前活动条目的完成比例 0..1；detail 为速率/字节等短文案。

        detail 只有在「正好一行在传」时才写进那一行——它描述的是那一个
        条目。多行同时在传（如一条 tar 流里的一整批）时这个数字是整批的
        总量，写进每一行就成了 64 行一模一样的 "129 MB / 162.8 MB"；
        这种情况下它归到阶段行，行上只写「进行中…」。
        """
        self._frac = min(1.0, max(0.0, float(fraction)))
        if detail:
            running = [i for i in self._active
                       if self._states[i] == STATE_RUNNING]
            if len(running) == 1:
                self._set_status_text(running[0], detail)
                self._aggregate = ""
            else:
                self._aggregate = detail
            self._refresh_stage()
        self._sync_summary()

    def finish_row(self, index: int, error: Optional[str] = None):
        if not (0 <= index < len(self._states)):
            return
        if self._states[index] in (STATE_DONE, STATE_FAILED, STATE_SKIPPED):
            return
        if error:
            self._states[index] = STATE_FAILED
            self._errors[index] = error
            self._set_status_text(index, t("transfer.state_failed",
                                           error=error))
            item = self._tree.topLevelItem(index)
            if item is not None:
                color = self._error_color()
                item.setForeground(0, color)
                item.setForeground(1, color)
                item.setToolTip(1, error)
        else:
            self._states[index] = STATE_DONE
            self._set_status_text(index, t("transfer.state_done"))
        if index in self._active:
            self._active = [i for i in self._active if i != index]
            self._frac = 0.0
        self._sync_summary()

    def finish_rows(self, indices, error_by_index: Optional[dict] = None):
        for i in indices:
            self.finish_row(i, (error_by_index or {}).get(i))

    # ---------- 收起 / 找回 ----------

    def hide_for_now(self):
        """收起窗口，传输继续跑（面板上的按钮可以把它叫回来）。"""
        if self._finished:
            self.close()
            return
        self._user_hidden = True
        self.hide()
        self.visibility_changed.emit(False)

    def yield_for_modal(self):
        """有模态框要弹了：先把自己藏起来，别压在人家上面。

        为什么是「藏」而不是「临时去掉置顶」：改 window flag 会销毁并重建
        原生窗口，在模态状态下做这件事会直接段错误（CI 上 macOS 与 Linux
        各崩过一次）。hide/show 不碰窗口句柄，安全且效果一样明确。
        """
        if self._finished or self._user_hidden or not self.isVisible():
            return
        self._auto_hidden = True
        self.hide()

    def restore_after_modal(self):
        """模态框关掉了：把刚才自动藏起来的窗口放回来（用户手动收起的不动）。"""
        if not self._auto_hidden:
            return
        self._auto_hidden = False
        if self._finished or self._user_hidden:
            return
        self.show()
        self.raise_()

    def _sync_with_modals(self):
        """定时检查：应用里有模态框就让位，没有就回来。

        用 activeModalWidget（Qt 自己维护的状态）而不是 WindowBlocked /
        WindowUnblocked 事件 —— 那两个事件在 offscreen 平台压根不发，等于
        没法自动化验证。
        """
        if self._finished:
            return
        if QApplication.activeModalWidget() is not None:
            self.yield_for_modal()
        else:
            self.restore_after_modal()

    def reopen(self):
        """把收起的窗口叫回来并顶到最前。"""
        self._user_hidden = False
        self.show()
        self.raise_()
        self.activateWindow()
        self.visibility_changed.emit(True)

    def finish_all(self):
        """收尾：没失败就自动关窗；有失败则留窗（收起过也叫回来），按钮变「关闭」。"""
        self._finished = True
        # 收尾时还挂在 pending/running 上的都是没做成的（取消/中途 break）——
        # 一律记「已取消」，绝不把没验证过的条目标成已完成
        for i, state in enumerate(self._states):
            if state in (STATE_PENDING, STATE_RUNNING):
                self._states[i] = STATE_SKIPPED
                self._set_status_text(i, t("transfer.state_skipped"))
        self._active = []
        self._frac = 0.0
        self._modal_watch.stop()
        self._sync_summary()
        if not self.failures():
            # 不发 visibility_changed：那个信号的含义是「用户收起了，
            # 面板上亮个按钮好找回来」，任务都结束了没什么可找回的
            self.close()
            return
        self._bar.setValue(BAR_SCALE)
        self._stage_base = ""
        self._aggregate = ""
        self._refresh_stage()
        self._hide_button.hide()
        self._button.setEnabled(True)
        self._button.setText(t("transfer.close"))
        # 有失败必须让人看见，哪怕之前被收起来了
        self.show()
        self.raise_()

    # ---------- 查询 ----------

    def was_canceled(self) -> bool:
        return self._canceled

    def is_finished(self) -> bool:
        return self._finished

    def failures(self) -> dict:
        """{条目下标: 错误文案}"""
        return dict(self._errors)

    def row_states(self) -> list:
        return list(self._states)

    def stage_text(self) -> str:
        return self._stage.text()

    def row_status_text(self, index: int) -> str:
        item = self._tree.topLevelItem(index)
        return item.text(1) if item is not None else ""

    def overall_value(self) -> int:
        return self._bar.value()

    # ---------- 内部 ----------

    def _show_if_running(self):
        # 用户已经手动收起过就别再自作主张弹出来
        if not self._finished and not self._user_hidden:
            self.show()

    def _refresh_stage(self):
        """阶段行 = 阶段说明 + 整批统计；与顶部那句完全相同就不重复占一行。"""
        base = self._stage_base
        if base and base == self._header_text:
            base = ""
        self._stage.setText(" · ".join(p for p in (base, self._aggregate) if p))

    def _set_status_text(self, index: int, text: str):
        item = self._tree.topLevelItem(index)
        if item is not None:
            item.setText(1, text)

    def _error_color(self) -> QColor:
        """深/浅底各自可读的红（不写死 QSS，跟随当前调色板）。"""
        window = self.palette().color(QPalette.ColorRole.Window)
        return QColor("#ff7b72") if window.lightness() < 128 else QColor("#c0392b")

    def _settled_count(self) -> int:
        return sum(1 for s in self._states
                   if s in (STATE_DONE, STATE_FAILED, STATE_SKIPPED))

    def _sync_summary(self):
        total = len(self._states) or 1
        settled = self._settled_count()
        running = sum(1 for i in self._active
                      if self._states[i] == STATE_RUNNING)
        value = (settled + running * self._frac) / total
        self._bar.setValue(int(min(1.0, max(0.0, value)) * BAR_SCALE))
        failed = len(self._errors)
        if failed:
            summary = t("transfer.summary_failed", done=settled,
                        total=len(self._states), failed=failed)
        else:
            summary = t("transfer.summary_ok", done=settled,
                        total=len(self._states))
        self.setWindowTitle(f"{self._base_title} · {summary}")

    def _on_button(self):
        if self._finished:
            self.close()
            return
        self._request_cancel()

    def _request_cancel(self):
        if self._canceled:
            return
        self._canceled = True
        self._button.setEnabled(False)
        self._button.setText(t("transfer.canceling"))
        self.canceled.emit()

    # Esc / 关窗 = 收起（传输继续），不是取消 —— 取消是「取消」按钮的事，
    # 关个窗就把几十个文件的传输掐了太吓人。收尾之后照常关闭。
    def reject(self):                      # noqa: N802 — Qt 接口
        if not self._finished:
            self.hide_for_now()
            return
        super().reject()

    def closeEvent(self, event):           # noqa: N802 — Qt 回调
        if not self._finished:
            self.hide_for_now()
            event.ignore()
            return
        super().closeEvent(event)
