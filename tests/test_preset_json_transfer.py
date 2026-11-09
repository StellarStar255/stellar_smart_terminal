"""跨电脑快捷命令 JSON 迁移：编辑、顺序、合并和失败原子性。"""
import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication, QDialog

from dialogs import PresetDialog


class PresetJsonTransferTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.source = [{'name': '服务器', 'commands': ['ssh server', '  echo "你好"']}]
        self.manager = PresetDialog(self.source)
        self.addCleanup(self.manager.deleteLater)

    def test_copy_includes_edits_and_order_without_internal_ids(self):
        self.manager.name_edit.setText('新名称')
        self.manager.commands_edit.setPlainText('ssh other\n  echo "你好"')
        self.manager._add_preset()
        self.manager.preset_list.model().moveRow(
            self.manager.preset_list.rootIndex(), 1,
            self.manager.preset_list.rootIndex(), 0)
        self.manager.copy_json_btn.click()
        data = json.loads(self.app.clipboard().text())
        self.assertEqual(data, {'presets': self.manager.get_presets()})
        self.assertEqual(data['presets'][1], {
            'name': '新名称', 'commands': ['ssh other', '  echo "你好"']})
        self.assertNotIn('_id', self.app.clipboard().text())
        self.assertEqual(self.source[0]['name'], '服务器')

    def test_round_trip_merges_without_overwriting_and_skips_duplicates(self):
        other = {'name': '服务器', 'commands': ['ssh other']}
        payload = {'presets': [self.source[0], other, other]}
        popup = QDialog(self.manager)
        self.manager._apply_json_import(json.dumps(payload), popup)
        self.assertEqual(popup.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.manager.get_presets(), self.source + [other])
        self.assertEqual(self.manager.preset_list.count(), 2)
        self.manager.name_edit.setText('已导入并编辑')
        self.assertEqual(self.manager.get_presets()[1]['name'], '已导入并编辑')
        self.assertEqual(self.source[0]['name'], '服务器')

    def test_invalid_input_does_not_partially_import(self):
        invalid = ['{', '{}', 'null', '[1]',
                   '[{"name": "bad", "commands": "echo hi"}]',
                   '[{"name": "bad", "commands": [42]}]',
                   json.dumps([{'name': 'valid', 'commands': []}, {'name': 'bad'}])]
        with patch('dialogs.QMessageBox.warning') as warning:
            for text in invalid:
                popup = QDialog(self.manager)
                self.manager._apply_json_import(text, popup)
                self.assertEqual(self.manager.get_presets(), self.source)
                self.assertEqual(popup.result(), QDialog.DialogCode.Rejected)
            self.assertEqual(warning.call_count, len(invalid))

    def test_accepts_local_file_format_and_plain_array(self):
        for payload in (self.source, {'presets': self.source}):
            self.assertEqual(PresetDialog._parse_presets_json(json.dumps(payload)), self.source)
        self.assertEqual(PresetDialog._parse_presets_json('[]'), [])

    def test_import_regenerates_ids_and_cancel_leaves_original_untouched(self):
        popup = QDialog(self.manager)
        self.manager._apply_json_import(json.dumps([
            {'_id': 1, 'name': 'imported', 'commands': ['echo hi']}]), popup)
        ids = [preset['_id'] for preset in self.manager.presets]
        self.assertEqual(len(set(ids)), 2)
        self.manager.reject()
        self.assertEqual(self.source, [
            {'name': '服务器', 'commands': ['ssh server', '  echo "你好"']}])


if __name__ == '__main__':
    unittest.main()
