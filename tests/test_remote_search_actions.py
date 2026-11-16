"""Search result file operations use the visible selection and real remote paths."""
from concurrent.futures import Future
from unittest import mock

from PyQt6.QtCore import QPoint, Qt, QItemSelectionModel
from PyQt6.QtGui import QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMenu, QMessageBox

import explorer_clipboard
from i18n import t
from ssh_session import RemoteEntry
from tests.test_remote_explorer_logic import _Base


class TestSearchActions(_Base):
    def setUp(self):
        super().setUp()
        explorer_clipboard.clear()
        self.app.clipboard().clear()
        self.addCleanup(explorer_clipboard.clear)
        self.addCleanup(self.app.clipboard().clear)
        self.panel = self._panel()
        self.addCleanup(self.panel.close)
        self.panel._session = mock.Mock()
        self.panel._session.host_config.alias = 'test-host'
        self.panel._current_path = '/work'
        self.panel._stack.setCurrentWidget(self.panel._tree_page)
        self.panel._search_edit.setText('match')
        self.panel._search_timer.stop()
        self.panel._tree.hide()
        self.panel._search_results.show()
        self.entries = [RemoteEntry('match.txt', '/work/nested/match.txt', False),
                        RemoteEntry('match2.txt', '/work/other/match2.txt', False)]
        self.panel._on_search_results(self.panel._search_gen, self.entries, False)
        # A stale selection in the hidden tree must never be copied/deleted/pasted to.
        stale = self.panel._make_item(RemoteEntry('old', '/wrong/old', True))
        self.panel._tree.addTopLevelItem(stale)
        stale.setSelected(True)

    def select(self, *rows):
        view = self.panel._search_results
        view.clearSelection()
        for row in rows:
            view.item(row).setSelected(True)
        if rows:
            view.setCurrentRow(rows[-1], QItemSelectionModel.SelectionFlag.NoUpdate)

    def menu_action(self, row, label):
        panel = self.panel
        item = panel._search_results.item(row) if row is not None else None
        def execute(menu, *_args):
            actions = [action for action in menu.actions() if action.text() == label]
            self.assertEqual(len(actions), 1, [a.text() for a in menu.actions()])
            actions[0].trigger()
        with mock.patch.object(panel._search_results, 'itemAt', return_value=item), \
                mock.patch.object(QMenu, 'exec', execute):
            panel._on_search_context_menu(QPoint())

    def test_context_menu_signal_is_connected_and_copy_uses_clicked_entry(self):
        panel = self.panel
        self.select(1)
        with mock.patch.object(panel, '_begin_clipboard_staging') as stage:
            self.menu_action(0, t('remote.copy'))
        self.assertEqual([item[2] for item in explorer_clipboard.effective_items()],
                         ['/work/nested/match.txt'])
        stage.assert_called_once()
        self.assertEqual(panel._search_results.contextMenuPolicy(),
                         Qt.ContextMenuPolicy.CustomContextMenu)
        with mock.patch.object(panel, '_show_file_context_menu') as show:
            panel._search_results.customContextMenuRequested.emit(QPoint())
        show.assert_called_once()

    def test_multiselect_copy_survives_streaming_update(self):
        panel = self.panel
        self.select(0, 1)
        extra = RemoteEntry('match3.txt', '/work/match3.txt', False)
        panel._on_search_results(panel._search_gen, self.entries + [extra], False, True)
        self.assertEqual({e.path for e, _ in panel._selected_file_entries()},
                         {e.path for e in self.entries})
        with mock.patch.object(panel, '_begin_clipboard_staging'):
            self.menu_action(1, t('remote.copy'))
        self.assertEqual({item[2] for item in explorer_clipboard.effective_items()},
                         {e.path for e in self.entries})

    def test_paste_shortcut_targets_file_parent_directory_or_search_root(self):
        panel = self.panel
        with mock.patch.object(panel, '_clipboard_paste_into') as paste:
            self.select(0)
            panel._paste_via_shortcut()
            paste.assert_called_with('/work/nested')
            folder = RemoteEntry('matchdir', '/work/deep/matchdir', True)
            panel._on_search_results(panel._search_gen, [folder], False)
            self.select(0)
            panel._paste_via_shortcut()
            paste.assert_called_with(folder.path)
            self.select()
            panel._paste_via_shortcut()
            paste.assert_called_with('/work')

    def test_context_paste_uses_nested_file_parent_and_blank_uses_root(self):
        panel = self.panel
        with mock.patch('explorer_clipboard.has_pastable', return_value=True), \
                mock.patch('explorer_clipboard.describe', return_value='files'), \
                mock.patch.object(panel, '_clipboard_paste_into') as paste:
            label = t('remote.paste_with_label', label='files')
            self.menu_action(0, label)
            paste.assert_called_with('/work/nested')
            self.menu_action(None, label)
            paste.assert_called_with('/work')

    def test_actual_copy_paste_keys_on_result_view_leave_search_input_text(self):
        panel = self.panel
        panel.show()
        panel.activateWindow()
        panel._search_results.setFocus()
        self.select(0)
        self.app.processEvents()
        with mock.patch.object(panel, '_begin_clipboard_staging'), \
                mock.patch.object(panel, '_clipboard_paste_into') as paste:
            QTest.keySequence(panel._search_results, QKeySequence(QKeySequence.StandardKey.Copy))
            self.app.processEvents()
            self.assertEqual([item[2] for item in explorer_clipboard.effective_items()],
                             ['/work/nested/match.txt'])
            QTest.keySequence(panel._search_results, QKeySequence(QKeySequence.StandardKey.Paste))
            self.app.processEvents()
            paste.assert_called_once_with('/work/nested')
        # Native text shortcuts in the input must not invoke file operations.
        panel._search_edit.setFocus()
        panel._search_edit.selectAll()
        with mock.patch.object(panel, '_clipboard_copy_selection') as copy:
            QTest.keySequence(panel._search_edit, QKeySequence(QKeySequence.StandardKey.Copy))
            self.app.processEvents()
            copy.assert_not_called()
        self.assertEqual(self.app.clipboard().text(), 'match')

    def test_rename_nested_result_preserves_query_and_refreshes_search(self):
        panel = self.panel
        future = Future()
        panel._session.submit.return_value = future
        with mock.patch('remote_explorer_widget.QInputDialog.getText',
                        return_value=('renamed.txt', True)):
            self.menu_action(0, t('remote.rename'))
        panel._session.submit.assert_called_once_with(
            panel._session.rename, '/work/nested/match.txt', '/work/nested/renamed.txt')
        with mock.patch.object(panel, '_populate_tree_root') as populate:
            future.set_result(None)
            self.app.processEvents()
            populate.assert_not_called()
        self.assertEqual(panel._search_edit.text(), 'match')
        self.assertTrue(panel._search_timer.isActive())

    def test_delete_multiselection_and_download_action_pass_real_targets(self):
        panel = self.panel
        self.select(0, 1)
        with mock.patch.object(panel, '_download_entries_to_local') as download:
            self.menu_action(0, t('remote.download'))
        self.assertEqual([e.path for e, _ in download.call_args.args[0]],
                         [e.path for e in self.entries])
        pending = [Future(), Future()]
        panel._session.submit.side_effect = pending
        with mock.patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            self.menu_action(0, t('remote.delete'))
        self.assertEqual([call.args[1] for call in panel._session.submit.call_args_list],
                         [e.path for e in self.entries])
        for future in pending:
            future.set_result(None)
        self.assertEqual(panel._search_edit.text(), 'match')
        self.assertTrue(panel._search_timer.isActive())

    def test_refresh_upload_and_copy_path_keep_search_context(self):
        panel = self.panel
        self.menu_action(0, t('remote.copy_path'))
        self.assertEqual(self.app.clipboard().text(), '/work/nested/match.txt')
        panel._on_refresh()
        panel._session.invalidate_cache.assert_called_once_with(None)
        self.assertEqual(panel._search_edit.text(), 'match')
        with mock.patch.object(panel, '_populate_tree_root') as populate:
            panel._refresh_upload_target(None, '/work/deep')
        populate.assert_not_called()
        self.assertTrue(panel._search_timer.isActive())

    def test_copy_then_paste_same_folder_uses_duplicate_name_and_keeps_search(self):
        panel = self.panel
        self.select(0)
        with mock.patch.object(panel, '_begin_clipboard_staging'):
            panel._clipboard_copy_selection()
        with mock.patch.object(panel, '_begin_transfer_job', return_value=None), \
                mock.patch.object(panel, '_remote_listing_entries',
                                  return_value={'match.txt': self.entries[0]}), \
                mock.patch.object(panel, '_remote_to_remote') as transfer, \
                mock.patch.object(panel, '_populate_tree_root') as populate:
            panel._clipboard_paste_into('/work/nested')
        transfer.assert_called_once_with(
            panel._session, panel._session,
            '/work/nested/match.txt', '/work/nested/match (1).txt')
        populate.assert_not_called()
        self.assertEqual(panel._search_edit.text(), 'match')
        self.assertTrue(panel._search_timer.isActive())

    def test_folder_download_and_open_containing_folder(self):
        panel = self.panel
        with mock.patch.object(panel, '_navigate_to_path') as navigate:
            self.menu_action(0, t('remote.open_containing_folder'))
        navigate.assert_called_once_with('/work/nested')
        folder = RemoteEntry('matchdir', '/work/deep/matchdir', True)
        panel._on_search_results(panel._search_gen, [folder], False)
        with mock.patch.object(panel, '_download_entries_to_local') as download:
            self.menu_action(0, t('remote.download'))
        download.assert_called_once_with([(folder, None)])

    def test_search_status_rows_do_not_copy_hidden_tree_selection(self):
        panel = self.panel
        panel._on_search_results(panel._search_gen, [], False, True)
        self.select(0)
        with mock.patch.object(panel, '_begin_clipboard_staging') as stage:
            panel._clipboard_copy_selection()
        stage.assert_not_called()
        self.assertEqual(panel._selected_file_entries(), [])
