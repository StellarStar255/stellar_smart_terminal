"""设置导出/导入：把可移植的配置打包成单个 JSON，在多台机器间同步。

背景：用户在 mac/Ubuntu/Windows 多机使用，预设/主题/快捷键各改各的很快
分裂（历史上还闹过"预设丢失"实为双配置分裂的乌龙）。这里只搬**可移植**
的键——机器相关的（工作目录历史、窗口几何、分割条尺寸等）一律不进包，
导入到另一台机器上也不会带坏本机布局。

注意：llm_configs 里含 API key，导出文件应按私密文件对待（导出对话框
的提示文案里已注明）。
"""
import json
import sys
import time
from pathlib import Path

import app_config
from app_logging import get_logger
from config_schema import PORTABLE_KEYS, validate_settings
from file_persistence import atomic_writer

logger = get_logger(__name__)

FORMAT_VERSION = 1



def export_settings(path) -> int:
    """把当前配置中的可移植键写到 path（JSON）。返回导出的键数。

    磁盘写失败向上抛异常（调用方弹窗提示）。
    """
    cfg = app_config.read_config()
    settings = {k: cfg[k] for k in PORTABLE_KEYS if k in cfg}
    payload = {
        'stellar_settings_export': FORMAT_VERSION,
        'exported_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'platform': sys.platform,
        'settings': settings,
    }
    validate_settings(settings)
    with atomic_writer(path, prefix='.settings_') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    logger.info("settings exported: %d keys -> %s", len(settings), path)
    return len(settings)


def import_settings(path) -> tuple[int, list]:
    """从 path 读入导出包并合并进本机配置。返回 (导入键数, 键名列表)。

    - 只接受本工具的导出格式（防止误选任意 JSON 覆盖配置）；
    - 只合并白名单键：旧版本导出的未知键、被篡改加入的键一律忽略；
    - 合并语义 = 逐键覆盖（app_config.update_config），不清空未涉及的键。

    格式不合法抛 ValueError；读盘/写盘失败抛 OSError。
    """
    raw = Path(path).read_text(encoding='utf-8')
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"not a valid JSON file: {e}") from e
    if not isinstance(payload, dict) or 'stellar_settings_export' not in payload:
        raise ValueError("not a Stellar settings export file")
    version = payload['stellar_settings_export']
    if type(version) is not int or version != FORMAT_VERSION:
        raise ValueError('unsupported settings export version')
    settings = payload.get('settings')
    if not isinstance(settings, dict):
        raise ValueError("malformed export: 'settings' missing")

    patch = {k: v for k, v in settings.items() if k in PORTABLE_KEYS}
    validate_settings(patch)
    if patch:
        ok = app_config.update_config(patch, description='settings import')
        if not ok:
            raise OSError("failed to write merged config")
    logger.info("settings imported: %d keys <- %s", len(patch), path)
    return len(patch), sorted(patch)
