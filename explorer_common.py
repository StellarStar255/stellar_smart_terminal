"""本地 / 远程文件浏览器的共享逻辑。

两个 explorer（explorer_widget 的本地视图、remote_explorer_widget 的 SSH
视图）数据模型不同，不宜强行合并成基类；但少数与视图无关的纯逻辑此前是
逐字复制粘贴、且已实际分叉出 bug。把这些收敛到这里单点维护。
"""
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
