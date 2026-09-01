"""本地 / 远程文件浏览器的共享逻辑。

两个 explorer（explorer_widget 的本地视图、remote_explorer_widget 的 SSH
视图）数据模型不同，不宜强行合并成基类；但少数与视图无关的纯逻辑此前是
逐字复制粘贴、且已实际分叉出 bug。把这些收敛到这里单点维护。
"""
import os
import posixpath
from typing import Optional, Tuple

from PyQt6.QtWidgets import QMessageBox, QCheckBox

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
