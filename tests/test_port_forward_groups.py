"""Port groups, batch state tracking, and persisted drag order (no SSH access)."""
import os
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PyQt6.QtCore import QEvent, QModelIndex, Qt
from PyQt6.QtWidgets import QApplication, QAbstractItemView
import app_config
from remote_explorer_widget import _ForwardsDialog, RemoteExplorerPanel

_APP = None


def rule(port):
    return dict(type='L', bind_host='127.0.0.1', bind_port=str(port),
                dest_host='127.0.0.1', dest_port=str(port), auto=False)


@pytest.fixture
def dialogs():
    global _APP
    _APP = QApplication.instance() or QApplication([])
    created = []
    def make(**kwargs):
        defaults = dict(alias='test-host', rules=[rule(p) for p in (6006, 7001, 8765)],
                        apply_cb=Mock(), cancel_cb=Mock())
        defaults.update(kwargs)
        d = _ForwardsDialog(**defaults)
        created.append(d)
        return d
    yield make
    for d in created:
        d.close()
        d.deleteLater()
    _APP.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _APP.processEvents()


def checked(d, *rows):
    d._check_all(False)
    for row in rows:
        d._list.item(row).setCheckState(Qt.CheckState.Checked)


def test_save_select_update_delete_named_groups(dialogs):
    save = Mock(return_value=True)
    d = dialogs(save_cb=save)
    checked(d, 0, 2)
    with patch('remote_explorer_widget.QInputDialog.getText', return_value=('Models', True)):
        d._save_group_as()
    keys = d._checked_keys()
    assert d.groups() == {'Models': keys}
    assert d.selected_group() == 'Models'
    save.assert_called_once_with(d.rules(), {'Models': keys}, 'Models')
    checked(d, 1)
    d._update_group()
    assert d.groups()['Models'] == d._checked_keys()
    d._check_all(True)
    d._group_combo.setCurrentIndex(0)
    d._group_combo.setCurrentIndex(d._group_combo.findData('Models'))
    assert d._checked_keys() == d.groups()['Models']
    d._apply_cb.assert_not_called()  # Choosing a group only selects checkboxes.
    d._delete_group()
    assert d.groups() == {}
    assert d._list.count() == 3


def test_group_start_stop_skips_active_and_leaves_other_ports_alone(dialogs):
    specs = [rule(p) for p in (6006, 7001, 8765)]
    key = _ForwardsDialog.rule_key
    d = dialogs(active={key(specs[0]), key(specs[2])})
    checked(d, 0, 1)
    d._apply_checked(False)
    d._apply_cb.assert_called_once_with(specs[1])
    assert d.active_keys() == {key(s) for s in specs}
    d._apply_checked(True)
    assert d._cancel_cb.call_count == 2
    assert d.active_keys() == {key(specs[2])}
    d._apply_checked(True)
    assert d._cancel_cb.call_count == 2


def test_partial_failure_preserves_truth_and_processes_remaining_rules(dialogs):
    def apply(spec):
        if spec['bind_port'] == '7001':
            raise RuntimeError('busy')
    d = dialogs(apply_cb=apply)
    d._check_all(True)
    with patch('remote_explorer_widget.QMessageBox.warning') as warning:
        d._apply_checked(False)
    warning.assert_called_once()
    assert d.active_keys() == {_ForwardsDialog.rule_key(rule(p)) for p in (6006, 8765)}
    assert '7001' in warning.call_args.args[2]
    d._cancel_cb = Mock(side_effect=RuntimeError('cancel failed'))
    with patch('remote_explorer_widget.QMessageBox.warning'):
        d._apply_checked(True)
    assert len(d.active_keys()) == 2
    assert not d._batch_running and d.isEnabled()


def test_reorder_preserves_group_membership_checked_and_active_state(dialogs):
    key = _ForwardsDialog.rule_key
    members = [key(rule(6006)), key(rule(8765))]
    save = Mock(return_value=True)
    d = dialogs(groups={'Models': members}, selected_group='Models',
                active={members[0]}, save_cb=save)
    assert d._list.dragDropMode() == QAbstractItemView.DragDropMode.InternalMove
    model = d._list.model()
    assert model.moveRows(QModelIndex(), 2, 1, QModelIndex(), 0)
    assert [s['bind_port'] for s in d.rules()] == ['8765', '6006', '7001']
    assert set(d._checked_keys()) == set(members)
    assert d.groups() == {'Models': members}
    assert d.active_keys() == {members[0]}
    assert save.call_args.args[0] == d.rules()
    d._apply_cb.assert_not_called()
    d._cancel_cb.assert_not_called()
    restored = dialogs(rules=save.call_args.args[0], groups=save.call_args.args[1],
                       selected_group=save.call_args.args[2])
    assert restored.rules() == d.rules()
    assert set(restored._checked_keys()) == set(members)


def test_delete_rule_prunes_membership_without_deleting_other_group_members(dialogs):
    key = _ForwardsDialog.rule_key
    members = [key(rule(6006)), key(rule(8765))]
    d = dialogs(groups={'Models': members}, selected_group='Models')
    d._list.setCurrentRow(0)
    d._on_delete()
    assert d.groups() == {'Models': [key(rule(8765))]}
    assert [s['bind_port'] for s in d.rules()] == ['7001', '8765']


def test_save_failure_does_not_claim_new_group_was_saved(dialogs):
    d = dialogs(save_cb=lambda *_: False)
    checked(d, 0)
    with patch('remote_explorer_widget.QInputDialog.getText', return_value=('Models', True)), \
            patch('remote_explorer_widget.QMessageBox.warning') as warning:
        d._save_group_as()
    warning.assert_called_once()
    assert d.groups() == {}


def test_duplicate_and_empty_groups_do_not_replace_saved_group(dialogs):
    key = _ForwardsDialog.rule_key(rule(6006))
    d = dialogs(groups={'Models': [key]}, selected_group='Models')
    checked(d, 1)
    with patch('remote_explorer_widget.QInputDialog.getText', return_value=('Models', True)), \
            patch('remote_explorer_widget.QMessageBox.warning'):
        d._save_group_as()
    assert d.groups() == {'Models': [key]}
    checked(d)
    with patch('remote_explorer_widget.QMessageBox.information'):
        d._update_group()
    assert d.groups() == {'Models': [key]}


def test_batch_blocks_reentry_and_dialog_close(dialogs):
    calls = []
    def apply(spec):
        d._apply_checked(False)
        d.accept()
        d.reject()
        calls.append(spec)
        assert d.result() == 0
    d = dialogs(apply_cb=apply)
    checked(d, 0)
    d._apply_checked(False)
    assert len(calls) == 1


def test_atomic_host_scoped_groups_order_and_other_settings_survive(tmp_path, dialogs):
    with patch.object(app_config, 'get_config_path', return_value=tmp_path / 'config.json'):
        app_config.update_config({'unrelated': 'keep'})
        panel = RemoteExplorerPanel(theme={})
        other = RemoteExplorerPanel(theme={})
        try:
            rules = [rule(8765), rule(6006)]
            groups = {'Models': [_ForwardsDialog.rule_key(rules[0])]}
            assert panel._save_forwards('a', rules, groups, 'Models')
            assert panel._save_forwards('b', [rule(7001)], {'Monitor': []}, 'Monitor')
            assert other._load_forwards('a') == rules
            assert other._load_forward_groups('a') == (groups, 'Models')
            assert other._load_forward_groups('b') == ({'Monitor': []}, 'Monitor')
            assert other._load_forward_groups('missing') == ({}, '')
            assert app_config.read_config()['unrelated'] == 'keep'
            panel._save_forwards('a', rules)  # Older caller must not erase group settings.
            assert other._load_forward_groups('a') == (groups, 'Models')
        finally:
            for w in (panel, other):
                w.close()
                w.deleteLater()
