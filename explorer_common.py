"""本地 / 远程文件浏览器的共享逻辑。

两个 explorer（explorer_widget 的本地视图、remote_explorer_widget 的 SSH
视图）数据模型不同，不宜强行合并成基类；但少数与视图无关的纯逻辑此前是
逐字复制粘贴、且已实际分叉出 bug。把这些收敛到这里单点维护。
"""
import os
import posixpath
import time
from collections import deque
from typing import Optional, Tuple

from PyQt6 import sip
from PyQt6.QtCore import Qt, QTimer, QEventLoop
from PyQt6.QtWidgets import QMessageBox, QCheckBox, QProgressDialog

from i18n import t
from transfer_progress import TransferProgressDialog
from app_logging import get_logger

logger = get_logger(__name__)


def _suspend_topmost_job(parent):
    """弹模态框前让 parent 面板上的传输窗口先藏起来；返回它以便还原。

    parent 不是 explorer 面板（或此刻没有传输）时返回 None。
    """
    getter = getattr(parent, "_active_transfer_job", None)
    if getter is None:
        return None
    try:
        job = getter()
        if job is None:
            return None
        job.yield_for_modal()
        return job
    except (RuntimeError, AttributeError):
        logger.debug("suspend topmost failed", exc_info=True)
        return None


def resolve_paste_conflict(parent, name: str,
                           sticky: Optional[str]) -> Optional[Tuple[str, bool]]:
    """粘贴目标已存在时的三选一对话框（覆盖 / 保留二者 / 取消）。

    本地和远程 explorer 语义完全一致，故共用。只依赖 parent 作为对话框父级，
    不触碰任何 explorer 特有状态。

    Args:
        parent: 对话框的父 QWidget。
        name: 冲突的条目名，用于提示文案。
        sticky: 若为 'overwrite'/'keep'（用户勾了"应用到剩余"）则直接复用，
            不再弹窗。

    Returns:
        ('overwrite', sticky_bool) — 覆盖
        ('keep',      sticky_bool) — 保留二者（调用方据此加 (N) 尾缀）
        None                        — 取消，中止剩余粘贴
    """
    if sticky in ("overwrite", "keep"):
        return (sticky, True)
    box = QMessageBox(parent)
    box.setWindowTitle(t("paste.conflict_title"))
    box.setText(t("paste.conflict_msg", name=name))
    box.setIcon(QMessageBox.Icon.Question)
    keep_btn = box.addButton(t("paste.btn_keep_both"), QMessageBox.ButtonRole.AcceptRole)
    overwrite_btn = box.addButton(t("paste.btn_overwrite"), QMessageBox.ButtonRole.DestructiveRole)
    cancel_btn = box.addButton(t("paste.btn_cancel"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep_btn)
    apply_all = QCheckBox(t("paste.apply_to_all"))
    box.setCheckBox(apply_all)
    # 传输进度窗口是置顶的，会盖住这个模态框让人点不到（按钮点不着 =
    # 整个粘贴卡死在这里）。弹框期间先让它藏起来，关掉再放回来。
    job = _suspend_topmost_job(parent)
    try:
        box.exec()
    finally:
        if job is not None:
            try:
                job.restore_after_modal()
            except RuntimeError:
                logger.debug("restore after modal: dialog gone", exc_info=True)
    clicked = box.clickedButton()
    if clicked is cancel_btn or clicked is None:
        return None
    action = "overwrite" if clicked is overwrite_btn else "keep"
    return (action, apply_all.isChecked())


# 编辑器无法有效展示、打开时应交给系统默认应用的扩展名。
# 名单之外的未知格式再按文件头是否含 NUL 字节嗅探二进制兜底。
SYSTEM_OPEN_EXTS = {
    # 办公文档
    '.xlsx', '.xls', '.docx', '.doc', '.pptx', '.ppt', '.pdf',
    '.key', '.numbers', '.pages', '.odt', '.ods', '.odp',
    # 压缩包/镜像
    '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz',
    '.dmg', '.iso', '.jar',
    # 音视频
    '.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg',
    '.mp4', '.mov', '.avi', '.mkv', '.webm',
    # 可执行/库/字体/数据库/设计稿
    '.exe', '.dll', '.dylib', '.so', '.bin', '.apk', '.ipa',
    '.ttf', '.otf', '.woff', '.woff2',
    '.sqlite', '.sqlite3', '.db', '.psd', '.ai', '.sketch',
}


# 字节级进度条的刻度（千分比）；不知道总字节时退化为「完成的任务数」
_BYTE_BAR_SCALE = 1000


class _TransferRateTracker:
    """传输测速：滑动窗口（最近 ~2s 的 (时刻, 累计字节) 采样）算瞬时速率，
    用全程平均速率估算剩余时间。按 key（当前文件的远端路径等）自动重置，
    多文件批量传输时显示的总是"当前这个文件"的速率。"""

    WINDOW_SECS = 2.0

    def __init__(self):
        self._samples: deque = deque()        # (monotonic_ts, bytes_done)
        self._key = None
        self._start: Optional[tuple] = None   # 首个采样 (ts, bytes)

    def reset(self, key=None):
        self._samples.clear()
        self._key = key
        self._start = None

    def update(self, key, bytes_done: int):
        if key != self._key:
            self.reset(key)
        now = time.monotonic()
        if self._start is None:
            self._start = (now, bytes_done)
        self._samples.append((now, bytes_done))
        cutoff = now - self.WINDOW_SECS
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def rate(self) -> float:
        """瞬时速率（bytes/s）；样本不足时返回 0（调用方不显示速率）。"""
        if len(self._samples) < 2:
            return 0.0
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        if t1 <= t0:
            return 0.0
        return max(0.0, (b1 - b0) / (t1 - t0))

    def eta_secs(self, bytes_done: int, bytes_total: int) -> Optional[float]:
        """按全程平均速率估算剩余秒数；速率未知 / 已完成返回 None。"""
        if self._start is None or bytes_total <= 0 or bytes_done >= bytes_total:
            return None
        t0, b0 = self._start
        elapsed = time.monotonic() - t0
        if elapsed <= 0 or bytes_done <= b0:
            return None
        avg = (bytes_done - b0) / elapsed
        if avg <= 0:
            return None
        return (bytes_total - bytes_done) / avg


class TransferJobHost:
    """给 explorer 面板接上「一批传输一个列表窗口」的能力（本地/远程共用）。

    以前一次粘贴里每个条目（远程侧还按 stat/download/upload 每个阶段）
    各弹一个 QProgressDialog，几十项就是几十次弹框闪烁，看不出整体进度、
    也说不清哪一项失败。面板混入这个类后：

        job = self._begin_transfer_job(names, header=...)   # < 2 项返回 None
        job 存在时各自的 _wait_future_with_progress 把进度画进窗口当前行，
        条目做完调 self._finish_job_row(job, row[, error])，
        整批收尾调 job.finish_all()（全绿自动关窗，有失败留窗逐行显示）。

    属性用类级默认值，面板不必在 __init__ 里初始化（QWidget 多继承时
    mixin 定义 __init__ 容易踩 MRO 的坑）。
    """

    # 一次粘贴至少这么多条目才开列表窗口；单条目还是老的单进度框
    # （一行的列表窗反而比进度条啰嗦）
    _JOB_MIN_ITEMS = 2

    _transfer_job: Optional[TransferProgressDialog] = None
    _job_abort_targets: list = []      # 只读默认；开新任务时换成实例列表

    def _begin_transfer_job(self, names: list, header: str,
                            title: str = "") -> Optional[TransferProgressDialog]:
        """给一批传输开一个统一进度窗口，并接上取消 → abort 会话。"""
        self._transfer_job = None
        self._job_abort_targets = []
        if len(names) < self._JOB_MIN_ITEMS:
            return None
        job = TransferProgressDialog(names, parent=self, title=title,
                                     header=header)
        # 「取消」只是请求优雅停（当前文件传完再停），绝不在这里关 socket ——
        # 关了远端那个文件就留半截。用户再按一次才走强制中断。
        job.force_canceled.connect(
            lambda: self._abort_sessions(self._job_abort_targets))
        # 窗口被收起 → 面板上亮出「传输进度」按钮，随时叫得回来
        job.visibility_changed.connect(
            lambda visible: self._set_transfer_chip_visible(not visible))
        self._transfer_job = job
        return job

    def _end_transfer_job(self, job) -> dict:
        """整批收尾：返回失败映射，清掉引用、收窗、灭掉面板上的按钮。"""
        from PyQt6 import sip

        self._transfer_job = None
        self._set_transfer_chip_visible(False)
        if job is None:
            return {}
        try:
            if sip.isdeleted(job):
                return {}
            failures = job.failures()
            job.finish_all()       # 全绿自动关窗；有失败则留窗逐行显示原因
            return failures
        except RuntimeError:
            logger.debug("_end_transfer_job: dialog gone", exc_info=True)
            return {}

    def _set_transfer_chip_visible(self, visible: bool):
        """面板上的「传输进度」按钮显隐；面板没有这个按钮就什么也不做。"""
        btn = getattr(self, "_transfer_chip", None)
        if btn is not None:
            btn.setVisible(bool(visible))

    def _reopen_transfer_job(self):
        job = self._active_transfer_job()
        if job is not None:
            job.reopen()

    def _active_transfer_job(self) -> Optional[TransferProgressDialog]:
        """当前批次的统一进度窗口；没有 / 已销毁 / 已收尾时返回 None。"""
        from PyQt6 import sip

        job = self._transfer_job
        if job is None:
            return None
        try:
            if sip.isdeleted(job) or job.is_finished():
                self._transfer_job = None
                return None
        except (RuntimeError, TypeError):
            self._transfer_job = None
            return None
        return job

    def _register_job_abort(self, job: TransferProgressDialog,
                            sessions: Optional[list]):
        """把本阶段可中断的会话登记到统一窗口的取消按钮上。

        点过取消之后才提交的阶段要立刻中断，否则「取消」只对当前这一个
        阶段生效，后面的条目照传不误。
        """
        for s in (sessions or []):
            if s is not None and all(s is not x for x in self._job_abort_targets):
                self._job_abort_targets.append(s)
        # 只有「强制停止」才关 socket；优雅停靠调用方在条目之间自己收手
        if job.was_force_canceled():
            self._abort_sessions(sessions)

    @staticmethod
    def _abort_sessions(sessions: Optional[list]):
        for s in (sessions or []):
            if s is not None:
                try:
                    s.abort()
                except Exception as e:      # noqa: BLE001 — 中断尽力而为
                    logger.debug(f"session abort failed: {e}")

    @staticmethod
    def _finish_job_row(job: Optional[TransferProgressDialog], row: int,
                        error: Optional[str] = None):
        """把某一行标成完成/失败（窗口可能已被销毁，安全跳过）。"""
        from PyQt6 import sip

        if job is None:
            return
        try:
            if not sip.isdeleted(job):
                job.finish_row(row, error)
        except RuntimeError:
            logger.debug("_finish_job_row: dialog gone", exc_info=True)

    def _await_remote(self, sess, fn, *args, label: str):
        """提交单个远端操作，在事件循环等待中返回结果。

        粘贴/覆盖删除流程里的 stat/listdir/remove 一律走这里，
        禁止直接 fut.result()——网络一慢就是整窗无限期冻结。
        """
        touch = getattr(self, "_touch_activity", None)   # 文件操作算用户活动
        if touch is not None:
            touch()
        fut = sess.submit(fn, *args)
        self._wait_future_with_progress([fut], label, abort_sessions=[sess])
        return fut.result()


    def _download_remote_recursive(self, sess, remote_path: str, local_path: str,
                                   label: Optional[str] = None):
        """通过 sess 把远程文件 / 目录递归下载到 local_path（阻塞，带进度）。

        label：进度文案；远程面板默认「粘贴到 <目标>」，本地面板传自己的。
        本地/远程 explorer 共用（以前是两份逐字相同的拷贝）。
        """
        if label is None:
            label = t("remote.pasting_progress", dst=local_path)
        entry = self._await_remote(sess, sess.stat, remote_path, label=label)
        if not entry.is_dir:
            fut = sess.submit(sess.download, remote_path, local_path)
            self._wait_future_with_progress([fut], label, abort_sessions=[sess])
            return
        os.makedirs(local_path, exist_ok=True)
        children = self._await_remote(sess, sess.listdir, remote_path, label=label)
        for child in children:
            child_local = os.path.join(local_path, child.name)
            if child.is_dir and not child.is_link:
                self._download_remote_recursive(sess, child.path, child_local,
                                                label=label)
            else:
                fut = sess.submit(sess.download, child.path, child_local)
                self._wait_future_with_progress([fut], label, abort_sessions=[sess])


    def _wait_future_with_progress(self, futures: list, label: str,
                                    tolerate_errors: bool = False,
                                    sizes: Optional[list] = None,
                                    live: Optional[dict] = None,
                                    abort_sessions: Optional[list] = None,
                                    on_bytes=None):
        """阻塞等待 futures 完成，跑事件循环避免 UI 卡死。

        sizes：与 futures 一一对应的字节数（未知填 0/None）。给出时进度框
        文案追加 "x MB / y MB · 速率 · 剩余时间"。

        live：可选的 {"bytes": int} 共享计数 —— upload_with_progress /
        download_with_progress 的字节级回调（worker 线程）往里写"当前
        正在传的这个文件已完成的字节数"，这里在主线程轮询时读出来叠加到
        已完成 future 的累计字节上，让单个大文件传输期间速率/ETA 也会动。
        不给 live 时退化为旧行为（按已完成文件粒度累计）。

        批量任务（粘贴一批文件）期间 self._transfer_job 是那一批的统一进度
        窗口：这里就不再新开 QProgressDialog，而是把阶段文案/比例画进窗口里
        当前那一行，全程只有一个窗口。

        本地与远程 explorer 共用同一份实现（以前本地是只按 future 个数计数的
        简化拷贝，远程→本地粘贴单个大文件时进度条一直 0/1）。"""
        if not futures:
            return
        total_bytes = sum(s or 0 for s in sizes) if sizes else 0
        job = self._active_transfer_job()
        progress = None
        bar_max = _BYTE_BAR_SCALE if total_bytes > 0 else len(futures)
        if job is not None:
            job.set_stage(label)
            self._register_job_abort(job, abort_sessions)
        else:
            # 有可中断的会话时给一个「取消」按钮：点了就 abort 这些会话，直接关 socket，
            # 让卡在 recv 上的传输立刻失败、对话框随即关闭，避免网络切换时一直卡在传输框里。
            cancel_text = t("remote.cancel_transfer") if abort_sessions else None
            # 进度条刻度：知道总字节时按字节走（千分比），否则退化为「完成的任务数」。
            # 按任务数在单任务传输（tar 整目录快路径、单个大文件）时永远是 0/1 ——
            # 条子空着直到传完瞬间跳满，看不出任何进度。
            progress = QProgressDialog(label, cancel_text, 0, bar_max, self)
            progress.setWindowTitle(t("remote.title"))
            # 非模态：大文件粘贴/传输期间应用可继续正常使用（后台传输），
            # 进度框只悬浮展示进度。等待仍走下面的局部事件循环，传输间的
            # 用户操作在嵌套循环里正常处理；同一 session 的操作由其单 worker
            # 线程天然串行，不会并发冲突。
            progress.setWindowModality(Qt.WindowModality.NonModal)
            progress.setMinimumDuration(300)
            progress.setValue(0)
            if abort_sessions:
                def _on_cancel(_sessions=list(abort_sessions)):
                    for s in _sessions:
                        if s is not None:
                            try:
                                s.abort()
                            except Exception as e:
                                logger.debug(f"session abort failed: {e}")
                progress.canceled.connect(_on_cancel)
        done = {"n": 0, "errors": [], "bytes": 0}
        tracker = _TransferRateTracker() if total_bytes > 0 else None
        # 进度文案节流（与 subtitle 路径的 350ms 节流一致）：QTimer 80ms
        # 一跳只更新进度条数值，label setText 限到 ~3Hz，避免布局抖动
        label_ts = {"t": 0.0}

        def make_cb(nbytes=0):
            def cb(f):
                # 优雅停时没轮到的 future 会被 cancel()：CancelledError 继承
                # 的是 BaseException，漏掉它这里就不会计数，等待循环永远不退
                if f.cancelled():
                    done["n"] += 1
                    return
                try:
                    f.result()
                except Exception as e:
                    done["errors"].append(str(e))
                done["n"] += 1
                # 当前文件收尾：live 里的"进行中字节"换成整文件累计，
                # 先清零再累加（瞬时少算一帧，比多算一帧不会倒退）
                if live is not None:
                    live["bytes"] = 0
                done["bytes"] += nbytes or 0
            return cb

        for i, fut in enumerate(futures):
            nbytes = sizes[i] if sizes and i < len(sizes) else 0
            fut.add_done_callback(make_cb(nbytes))

        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(80)
        def tick():
            # 父 widget 可能在等待期间被销毁（如用户关掉 panel/窗口），
            # 此时进度窗口也已 deleteLater'd → 任何访问都会段错误
            try:
                ui = progress if progress is not None else job
                if sip.isdeleted(ui):
                    # 面板在传输中被销毁：abort 会话让 pending futures 快速
                    # 失败，避免调用方后续 fut.result() 隐形阻塞主线程
                    for s in (abort_sessions or []):
                        if s is not None:
                            try:
                                s.abort()
                            except Exception:
                                logger.debug("abort on progress deletion failed",
                                             exc_info=True)
                    timer.stop()
                    loop.quit()
                    return
                # 传输进行中一律算活动，别让空闲看门狗掐掉正在传数据的连接
                # （只有远程面板有看门狗；本地面板没有这个方法）
                touch = getattr(self, "_touch_activity", None)
                if touch is not None:
                    touch()
                if job is not None and job.was_canceled():
                    # 优雅停：还没轮到的直接取消（cancel() 对已在跑的返回
                    # False，所以正在写的那个文件会照常传完，不会留半截）
                    for f in futures:
                        f.cancel()
                cur_bytes = done["bytes"] + (live["bytes"] if live else 0)
                if total_bytes > 0:
                    frac = min(1.0, cur_bytes / total_bytes)
                else:
                    frac = done["n"] / len(futures)
                if progress is not None:
                    progress.setValue(int(frac * bar_max))
                now = time.monotonic()
                detail = ""
                refresh_text = (tracker is not None and cur_bytes < total_bytes
                                and now - label_ts["t"] >= 0.35)
                if refresh_text:
                    label_ts["t"] = now
                    tracker.update("batch", cur_bytes)
                    detail = (f"{self._fmt_size(cur_bytes)}"
                              f" / {self._fmt_size(total_bytes)}")
                    stats = self._transfer_stats_text(
                        tracker, cur_bytes, total_bytes)
                    if stats:
                        detail += f" · {stats}"
                    if progress is not None:
                        progress.setLabelText(f"{label} · {detail}")
                if progress is None:
                    if on_bytes is not None:
                        # 调用方自己按累计字节维护行状态（哪个文件在传、传了
                        # 多少）；这里只把整批统计写到阶段行
                        if refresh_text:
                            job.set_stage(f"{label} · {detail}")
                        # 只在「还在传」的时候推进行状态：future 完成那一跳会
                        # 把字节计数直接顶到总量，**失败时也一样** —— 拿它标
                        # 「已完成」就会出现「1 秒 63 个 Done，其实一个没传」。
                        # 最终成败一律由等待结束后的收尾逻辑说了算。
                        if done["n"] < len(futures):
                            on_bytes(cur_bytes)
                    else:
                        # 统一窗口：进度条按「已完成条目 + 当前条目比例」推进，
                        # 速率/字节写进当前那一行的状态列
                        job.set_stage_progress(frac, detail)
                if done["n"] >= len(futures):
                    timer.stop()
                    loop.quit()
            except RuntimeError:
                timer.stop()
                loop.quit()
        timer.timeout.connect(tick)
        timer.start()
        loop.exec()
        try:
            if progress is not None and not sip.isdeleted(progress):
                progress.setValue(bar_max)   # 收尾拉满（关闭对话框）
            elif progress is None and not sip.isdeleted(job):
                job.set_stage_progress(1.0)
        except RuntimeError:  # Qt 对象已销毁（窗口/面板已关）
            pass
        if done["errors"] and not tolerate_errors:
            raise RuntimeError("; ".join(done["errors"]))


    @staticmethod
    def _fmt_size(n: int) -> str:
        if n is None or n < 0:
            return "?"
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        if n < 1024 * 1024 * 1024:
            return f"{n / (1024 * 1024):.1f} MB"
        return f"{n / (1024 * 1024 * 1024):.2f} GB"

    @classmethod
    def _fmt_rate(cls, bps: float) -> str:
        """速率文案：1.5 MB/s / 320.0 KB/s"""
        return f"{cls._fmt_size(int(bps))}/s"

    @staticmethod
    def _fmt_eta(seconds: float) -> str:
        """剩余时间 mm:ss（向最近秒取整）"""
        secs = max(0, int(seconds + 0.5))
        return f"{secs // 60}:{secs % 60:02d}"

    def _transfer_stats_text(self, tracker: "_TransferRateTracker",
                             bytes_done: int, bytes_total: int) -> str:
        """根据 tracker 生成 "1.5 MB/s · 剩余 0:04" 尾缀；速率未知返回空串。"""
        rate = tracker.rate()
        if rate <= 0:
            return ""
        eta = tracker.eta_secs(bytes_done, bytes_total)
        if eta is None:
            return self._fmt_rate(rate)
        return t("remote.transfer_stats",
                 rate=self._fmt_rate(rate), eta=self._fmt_eta(eta))


    @staticmethod
    def _clipboard_item_name(it) -> str:
        """剪贴板条目在列表窗口里显示的名字。"""
        kind = it[0] if it else ""
        if kind == "local":
            src = str(it[1])
            return os.path.basename(src.rstrip("/\\")) or src
        if kind == "remote":
            host_alias, remote_src = it[1], str(it[2])
            base = posixpath.basename(remote_src.rstrip("/")) or remote_src
            return f"{host_alias}:{base}" if host_alias else base
        return str(it)


def editor_can_display(file_path: str) -> bool:
    """内置编辑器能否有效展示该文件（文本/代码/图片可以）。

    展示不了的（office 文档、压缩包、音视频等二进制）应交给系统默认
    应用打开。未知扩展名按前 8KB 是否含 NUL 字节嗅探二进制。
    本地与远程 explorer 共用（远程侧对下载好的临时文件调用）。
    """
    # 惰性导入：file_editor 较重且仅此处用到，避免潜在的环形导入
    from file_editor import _IMAGE_EXTENSIONS

    ext = os.path.splitext(file_path)[1].lower()
    if ext in _IMAGE_EXTENSIONS:
        return True   # 编辑器有内联图片预览
    if ext in SYSTEM_OPEN_EXTS:
        return False
    try:
        with open(file_path, 'rb') as f:
            return b'\x00' not in f.read(8192)
    except OSError:
        return True   # 读不了仍走编辑器，沿用其现有报错提示
