# -*- coding: utf-8 -*-
"""设置导出/导入（settings_transfer.py）的测试

覆盖：
- 导出只含白名单键（机器相关键不外泄）+ 包裹格式
- 导入合并：白名单键覆盖、未知/机器相关键忽略、不清空未涉及的键
- 拒绝任意 JSON / 损坏文件（防误选文件覆盖配置）
- 往返（export → import）幂等

配置一律 patch app_config.get_config_path 到临时文件（与
test_update_restore_windows 同一套隔离约定）。纯逻辑无 Qt：
    python3 -m pytest tests/test_settings_transfer.py -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils
import settings_transfer
from settings_transfer import PORTABLE_KEYS, export_settings, import_settings


class TestSettingsTransfer(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='settings_transfer_')
        self._cfg_path = Path(self._tmp.name) / 'config.json'
        # settings_transfer 经 app_config 读写；app_config 经 utils.get_config_path
        self._orig_get_path = utils.get_config_path
        utils.get_config_path = lambda: self._cfg_path
        import app_config
        self._orig_ac_get_path = getattr(app_config, 'get_config_path', None)
        app_config.get_config_path = lambda: self._cfg_path

    def tearDown(self):
        import app_config
        utils.get_config_path = self._orig_get_path
        if self._orig_ac_get_path is not None:
            app_config.get_config_path = self._orig_ac_get_path
        self._tmp.cleanup()

    def _write_cfg(self, d):
        self._cfg_path.write_text(json.dumps(d), encoding='utf-8')

    def _read_cfg(self):
        return json.loads(self._cfg_path.read_text(encoding='utf-8'))

    def test_export_only_portable_keys(self):
        self._write_cfg({
            'presets': [{'name': 'zsh', 'commands': ['zsh']}],
            'theme': '午夜黑',
            'working_dir_history': ['/Users/me/secret-project'],
            'window_geometry': [1, 2, 3, 4],
        })
        out = Path(self._tmp.name) / 'export.json'
        n = export_settings(out)
        payload = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(payload['stellar_settings_export'], 1)
        self.assertEqual(n, 2)
        self.assertEqual(set(payload['settings']), {'presets', 'theme'})
        # 机器相关键绝不外泄
        self.assertNotIn('working_dir_history', payload['settings'])

    def test_import_merges_without_clearing_others(self):
        self._write_cfg({
            'theme': '旧主题',
            'working_dir_history': ['/local/path'],
            'git_proxy': 'http://127.0.0.1:7897',
        })
        pkg = Path(self._tmp.name) / 'pkg.json'
        pkg.write_text(json.dumps({
            'stellar_settings_export': 1,
            'settings': {
                'theme': '新主题',
                'presets': [{'name': 'p', 'commands': ['zsh']}],
                'working_dir_history': ['/other/machine/path'],  # 非法：应忽略
                'evil_unknown_key': 'x',
            },
        }), encoding='utf-8')
        n, keys = import_settings(pkg)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(keys), ['presets', 'theme'])
        cfg = self._read_cfg()
        self.assertEqual(cfg['theme'], '新主题')
        self.assertEqual(cfg['presets'][0]['name'], 'p')
        # 机器相关键保持本机值；未涉及的键不被清空
        self.assertEqual(cfg['working_dir_history'], ['/local/path'])
        self.assertEqual(cfg['git_proxy'], 'http://127.0.0.1:7897')
        self.assertNotIn('evil_unknown_key', cfg)

    def test_reject_arbitrary_json(self):
        bad = Path(self._tmp.name) / 'random.json'
        bad.write_text('{"theme": "x"}', encoding='utf-8')
        with self.assertRaises(ValueError):
            import_settings(bad)
        broken = Path(self._tmp.name) / 'broken.json'
        broken.write_text('{not json', encoding='utf-8')
        with self.assertRaises(ValueError):
            import_settings(broken)

    def test_roundtrip_idempotent(self):
        original = {
            'presets': [{'name': 'claude', 'commands': ['zsh', 'claude']}],
            'theme': '午夜黑',
            'keyboard_shortcuts': {'split_h': 'Ctrl+Shift+='},
            'terminal_scrollback': 5000,
        }
        self._write_cfg(dict(original))
        out = Path(self._tmp.name) / 'rt.json'
        export_settings(out)
        self._write_cfg({})  # 清空后导入
        n, _ = import_settings(out)
        self.assertEqual(n, len(original))
        cfg = self._read_cfg()
        for k, v in original.items():
            self.assertEqual(cfg[k], v)

    def test_portable_keys_have_no_machine_specific(self):
        for k in ('working_dir_history', 'window_geometry', 'last_working_dir',
                  'explorer_main_splitter_sizes', 'navigator_geometry'):
            self.assertNotIn(k, PORTABLE_KEYS)


if __name__ == '__main__':
    unittest.main()
