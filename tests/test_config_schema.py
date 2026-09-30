"""Settings validation is strict at import and non-destructive at load."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_config
import config_schema
import file_persistence
import settings_transfer
import utils


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text('{"theme":"old","machine_setting":42}', encoding='utf-8')
    monkeypatch.setattr(app_config, 'get_config_path', lambda: path)
    return path


@pytest.mark.parametrize('value', [[], [1], True, False, 1, 0, 'text', ''])
def test_invalid_config_root_is_rejected_without_overwrite(config_path, value):
    config_path.write_text(json.dumps(value), encoding='utf-8')
    original = config_path.read_bytes()
    assert utils.read_config_json(config_path) == ({}, False)
    assert app_config.read_config() == {}
    assert not app_config.update_config({'theme': 'new'})
    assert config_path.read_bytes() == original


@pytest.mark.parametrize('version', [0, 2, 999, True, 1.0, '1', None])
def test_unsupported_export_version_does_not_change_config(tmp_path, config_path, version):
    original = config_path.read_bytes()
    pkg = tmp_path / 'import.json'
    pkg.write_text(json.dumps({'stellar_settings_export': version,
                              'settings': {'theme': 'new'}}), encoding='utf-8')
    with pytest.raises(ValueError):
        settings_transfer.import_settings(pkg)
    assert config_path.read_bytes() == original


@pytest.mark.parametrize('settings', [
    {'gui_font_size': '12'}, {'gui_font_size': True}, {'gui_font_size': 33},
    {'window_opacity': 0}, {'window_opacity': 101},
    {'terminal_scrollback': 100001}, {'global_zoom_delta': 1.5},
    {'default_llm_config': -1}, {'language': 'unknown'},
    {'ai_completion_enabled': 'false'}, {'git_proxy': []},
    {'git_proxies': [1]}, {'presets': [None]}, {'presets': 'invalid'},
    {'presets': [{'commands': 'zsh'}]}, {'llm_configs': 'invalid'},
    {'llm_configs': [{'api_key': 123}]},
    {'llm_configs': [{'timeout': 0}]}, {'llm_configs': [{'max_tokens': -1}]},
    {'llm_configs': [{'temperature': float('nan')}]},
    {'llm_configs': [{'top_p': float('inf')}]},
    {'keyboard_shortcuts': {'save': []}}, {'toolbar_config': []},
    {'toolbar_config': {'button_order': {'group': 'bad'}}},
    {'toolbar_config': {'order_version': '2'}},
    {'output_alert_patterns': ['[']},
])
def test_bad_import_is_all_or_nothing(tmp_path, config_path, settings):
    original = config_path.read_bytes()
    pkg = tmp_path / 'import.json'
    pkg.write_text(json.dumps({'stellar_settings_export': 1,
                              'settings': {'theme': 'new', **settings}}), encoding='utf-8')
    with pytest.raises(ValueError):
        settings_transfer.import_settings(pkg)
    assert config_path.read_bytes() == original


def test_every_portable_field_roundtrips(tmp_path, config_path):
    settings = {key: True for key in config_schema._BOOL_KEYS}
    settings.update({
        'presets': [{'name': 'shell', 'commands': ['zsh'], 'future': 1}],
        'llm_configs': [{'name': 'model', 'api_base': 'https://example.invalid',
                         'api_key': 'dummy-secret', 'model': 'test', 'proxy': '',
                         'timeout': 30, 'max_tokens': 4096, 'temperature': 1.0, 'top_p': 1.0}],
        'default_llm_config': 0, 'theme': 'custom theme', 'language': 'zh',
        'gui_font_size': 0, 'global_zoom_delta': -3, 'window_opacity': 100,
        'keyboard_shortcuts': {'save': 'Ctrl+S'}, 'terminal_scrollback': 5000,
        'toolbar_config': {'layout': 'single', 'visible_buttons': {'save': True},
                           'button_order': {'group': ['save']}, 'group_order': ['group'],
                           'button_groups': {'save': 'group'}, 'order_version': 2},
        'notify_sound': '', 'output_alert_patterns': ['error.*'],
        'git_proxy': '', 'git_proxies': ['http://localhost:7897'],
    })
    assert set(settings) == set(settings_transfer.PORTABLE_KEYS)
    config_path.write_text(json.dumps(settings), encoding='utf-8')
    export = tmp_path / 'export.json'
    assert settings_transfer.export_settings(export) == len(settings)
    config_path.write_text('{}', encoding='utf-8')
    settings_transfer.import_settings(export)
    assert json.loads(config_path.read_text()) == settings


def test_invalid_existing_fields_use_defaults_without_rewriting(config_path):
    raw = {'gui_font_size': 'bad', 'llm_configs': 'bad', 'theme': 'valid', 'future': 1}
    config_path.write_text(json.dumps(raw), encoding='utf-8')
    original = config_path.read_bytes()
    assert app_config.read_config() == {'theme': 'valid', 'future': 1}
    assert config_path.read_bytes() == original


@pytest.mark.skipif(os.name != 'posix', reason='POSIX file modes')
def test_export_is_private_even_when_replacing_public_file(tmp_path, config_path):
    config_path.write_text(json.dumps({'llm_configs': [{'api_key': 'dummy-secret'}]}))
    export = tmp_path / 'export.json'
    for existing in (False, True):
        if existing:
            export.chmod(0o644)
        old_umask = os.umask(0o022)
        try:
            settings_transfer.export_settings(export)
        finally:
            os.umask(old_umask)
        assert stat.S_IMODE(export.stat().st_mode) == 0o600


@pytest.mark.parametrize('failure', ['write', 'replace'])
def test_failed_export_preserves_previous_file(tmp_path, config_path, monkeypatch, failure):
    export = tmp_path / 'export.json'
    export.write_bytes(b'previous export')

    def fail_write(data, stream, **kwargs):
        stream.write('partial')
        raise OSError('disk full')

    def fail_replace(*args):
        raise PermissionError('locked')

    if failure == 'write':
        monkeypatch.setattr(settings_transfer.json, 'dump', fail_write)
    else:
        monkeypatch.setattr(file_persistence.os, 'replace', fail_replace)
    with pytest.raises(OSError):
        settings_transfer.export_settings(export)
    assert export.read_bytes() == b'previous export'
    assert not list(tmp_path.glob('.settings_*.tmp'))
