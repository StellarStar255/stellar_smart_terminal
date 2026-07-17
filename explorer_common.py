"""本地 / 远程文件浏览器的共享逻辑。

两个 explorer（explorer_widget 的本地视图、remote_explorer_widget 的 SSH
视图）数据模型不同，不宜强行合并成基类；但少数与视图无关的纯逻辑此前是
逐字复制粘贴、且已实际分叉出 bug。把这些收敛到这里单点维护。
"""
import os
from typing import Optional, Tuple

from PyQt6.QtWidgets import QMessageBox, QCheckBox

from i18n import t


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
    box.exec()
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
