"""Qt-independent schema for portable settings, shared by import and export.

Unknown keys remain forward compatible. Known values are checked without
coercion; error messages identify fields but never include secret values.
"""
import math
import re

# 可移植键白名单。新增设置项时：跨机器通用的加进来，机器相关的别加。
PORTABLE_KEYS = (
    # 预设与 LLM
    'presets',
    'llm_configs',
    'default_llm_config',
    # 外观
    'theme',
    'icon_tint',
    'language',
    'gui_font_size',
    'global_zoom_delta',
    'window_opacity',
    # 键位与工具栏布局
    'keyboard_shortcuts',
    'toolbar_config',
    # 终端与行为开关
    'terminal_scrollback',
    'notify_sound',
    'parse_on_reader_thread',
    'mouse_click_forward_enabled',
    'spring_mode_enabled',
    'explorer_split_horizontal',
    'remote_split_horizontal',
    'editor_word_wrap',
    'ai_completion_enabled',
    'navigator_enabled',
    'image_prefix_enabled',
    'image_save_local',
    'auto_update_check',
    'workspace_restore_enabled',
    'output_alert_enabled',
    'output_alert_patterns',
    # Git 代理
    'git_proxy',
    'git_proxies',
)


_BOOL_KEYS = {
    'icon_tint', 'parse_on_reader_thread', 'mouse_click_forward_enabled',
    'spring_mode_enabled', 'explorer_split_horizontal', 'remote_split_horizontal',
    'editor_word_wrap', 'ai_completion_enabled', 'navigator_enabled',
    'image_prefix_enabled', 'image_save_local', 'auto_update_check',
    'workspace_restore_enabled', 'output_alert_enabled',
}


def _require(valid, field, expected):
    if not valid:
        raise ValueError(f"invalid setting {field}: expected {expected}")


def _strings(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _mapping(value, predicate):
    return isinstance(value, dict) and all(
        isinstance(k, str) and predicate(v) for k, v in value.items())


def _number(value, low, high=None, *, integer=False):
    kinds = (int,) if integer else (int, float)
    return (type(value) in kinds and (type(value) is int or math.isfinite(value))
            and value >= low
            and (high is None or value <= high))


def _records(value, field, llm=False):
    _require(isinstance(value, list), field, 'list of objects')
    for i, record in enumerate(value):
        name = f'{field}[{i}]'
        _require(isinstance(record, dict), name, 'object')
        strings = ('name', 'api_base', 'api_key', 'model', 'proxy') if llm else ('name',)
        for key in strings:
            if key in record:
                _require(isinstance(record[key], str), f'{name}.{key}', 'string')
        if not llm and 'commands' in record:
            _require(_strings(record['commands']), f'{name}.commands', 'list of strings')
        if llm:
            for key in ('timeout', 'max_tokens'):
                if key in record:
                    _require(_number(record[key], 1, integer=True),
                             f'{name}.{key}', 'positive integer')
            for key, maximum in (('temperature', 2), ('top_p', 1)):
                if key in record:
                    _require(_number(record[key], 0, maximum),
                             f'{name}.{key}', f'finite number in [0, {maximum}]')


def _toolbar(value):
    if value is None:  # legacy default: use built-in layout
        return
    _require(isinstance(value, dict), 'toolbar_config', 'object or null')
    checks = {
        'layout': lambda v: v in ('single', 'double'),
        'visible_buttons': lambda v: _mapping(v, lambda x: type(x) is bool),
        'button_order': lambda v: _mapping(v, _strings),
        'group_order': _strings,
        'button_groups': lambda v: _mapping(v, lambda x: isinstance(x, str)),
        'order_version': lambda v: _number(v, 0, integer=True),
    }
    for key, check in checks.items():
        if key in value:
            _require(check(value[key]), f'toolbar_config.{key}', 'valid toolbar value')


def validate_settings(settings):
    """Validate known portable fields; raise ValueError before any writes."""
    _require(isinstance(settings, dict), 'settings', 'object')
    for key, value in settings.items():
        if key in _BOOL_KEYS:
            _require(type(value) is bool, key, 'boolean')
        elif key in ('theme', 'notify_sound', 'git_proxy'):
            _require(isinstance(value, str), key, 'string')
        elif key == 'language':
            _require(value in ('zh', 'en'), key, 'zh or en')
        elif key == 'gui_font_size':
            _require(type(value) is int and (value == 0 or 8 <= value <= 32),
                     key, '0 (auto) or integer in [8, 32]')
        elif key == 'window_opacity':
            _require(_number(value, 10, 100, integer=True), key, 'integer in [10, 100]')
        elif key == 'terminal_scrollback':
            _require(_number(value, 500, 100000, integer=True), key, 'integer in [500, 100000]')
        elif key == 'global_zoom_delta':
            _require(type(value) is int, key, 'integer')
        elif key == 'default_llm_config':
            _require(_number(value, 0, integer=True), key, 'nonnegative integer')
        elif key in ('presets', 'llm_configs'):
            _records(value, key, llm=key == 'llm_configs')
        elif key == 'keyboard_shortcuts':
            _require(_mapping(value, lambda x: isinstance(x, str)), key, 'string map')
        elif key == 'toolbar_config':
            _toolbar(value)
        elif key in ('git_proxies', 'output_alert_patterns'):
            _require(_strings(value), key, 'list of strings')
            if key == 'output_alert_patterns':
                for i, pattern in enumerate(value):
                    try:
                        re.compile(pattern)
                    except re.error:
                        raise ValueError(f'invalid setting {key}[{i}]: invalid regex') from None


def config_for_read(config):
    """Return a usable copy plus invalid field names; never rewrite source data.

    Existing configurations retain unknown fields. Invalid portable values fall
    back to each caller's normal defaults without discarding unrelated settings.
    """
    result = dict(config)
    invalid = []
    for key in PORTABLE_KEYS:
        if key not in result:
            continue
        try:
            validate_settings({key: result[key]})
        except ValueError:
            invalid.append(key)
            del result[key]
    return result, invalid
