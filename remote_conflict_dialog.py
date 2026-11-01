"""Compare a retained local edit with the current remote text."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                             QPlainTextEdit, QSplitter, QVBoxLayout, QWidget)
from i18n import t


def merge_remote_contents(parent, local_text, remote_text, *, remote_missing=False):
    dialog = QDialog(parent)
    dialog.setWindowTitle(t('remote.sync_compare'))
    dialog.resize(1000, 650)
    layout = QVBoxLayout(dialog)
    hint = QLabel(t('remote.sync_deleted_hint' if remote_missing else 'remote.sync_merge_hint'))
    hint.setWordWrap(True)
    layout.addWidget(hint)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    editors = []
    for title, text, readonly in [(t('remote.sync_local'), local_text, False),
                                   (t('remote.sync_deleted' if remote_missing else 'remote.sync_remote'), remote_text, True)]:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.addWidget(QLabel(title))
        editor = QPlainTextEdit(text)
        editor.setReadOnly(readonly)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        column.addWidget(editor)
        splitter.addWidget(panel)
        editors.append(editor)
    layout.addWidget(splitter, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                               QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText(t('remote.sync_restore' if remote_missing else 'remote.sync_merge_save'))
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t('remote.sync_merge_cancel'))
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    footer = QHBoxLayout()
    footer.addWidget(buttons)
    layout.addLayout(footer)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return editors[0].toPlainText()
    return None
