"""
统一日志配置 (Smart Terminal)

用法:
    from app_logging import get_logger
    logger = get_logger(__name__)

入口处（app.py main()）调用一次 setup_logging() 即可：
- 文件日志: ~/.smart_terminal_logs/smart_terminal.log（RotatingFileHandler，
  2MB x 3 备份）。SMART_TERMINAL_DEBUG=1 时记录 DEBUG，否则 INFO。
- 终端输出: 仅 WARNING 及以上，避免刷屏干扰嵌入式终端的正常使用。

注意：本模块不能命名为 logging.py，否则会遮蔽标准库。
"""

import logging
import logging.handlers
import os
from pathlib import Path

LOG_DIR = Path.home() / ".smart_terminal_logs"
LOG_FILE = LOG_DIR / "smart_terminal.log"

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# setup_logging 幂等标记：避免重复调用时叠加 handler
_configured = False


def setup_logging() -> None:
    """配置根 logger（幂等，重复调用不会叠加 handler）。"""
    global _configured
    if _configured:
        return
    _configured = True

    debug = os.environ.get("SMART_TERMINAL_DEBUG") == "1"
    file_level = logging.DEBUG if debug else logging.INFO

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 实际级别由各 handler 控制

    formatter = logging.Formatter(_FORMAT)

    # 文件 handler：滚动日志，目录权限 0o700（日志可能含路径等隐私信息）
    try:
        LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        # 日志目录/文件不可写时不阻断程序启动，仅保留终端输出
        pass

    # 终端 handler：只输出 WARNING 及以上，避免刷终端
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)


def get_logger(name: str) -> logging.Logger:
    """获取模块 logger 的薄包装。"""
    return logging.getLogger(name)
