"""
工具函数模块
"""
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Set

# 支持的文件扩展名
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.ico'}
DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv'}
CODE_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.json', '.yaml', '.yml', '.md'}

ALL_EXTENSIONS = IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS | CODE_EXTENSIONS

# 预编译文件路径匹配正则表达式
_ext_pattern = '|'.join(ext[1:] for ext in ALL_EXTENSIONS)
# Unix absolute paths: /home/user/file.py
_RE_UNIX_ABS_PATH = re.compile(r'(/[^\s:*?"<>|\r\n]+\.(?:' + _ext_pattern + r'))', re.IGNORECASE)
# Windows absolute paths: C:\Users\file.py or D:/path/file.py
_RE_WIN_ABS_PATH = re.compile(r'([A-Za-z]:[\\\/][^\s:*?"<>|\r\n]*\.(?:' + _ext_pattern + r'))', re.IGNORECASE)
_RE_REL_PATH = re.compile(r'(?:^|[\s(])([./]?[\w\-./\\]+\.(?:' + _ext_pattern + r'))', re.IGNORECASE)


def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent


def get_sessions_dir() -> Path:
    """获取会话存储目录"""
    sessions_dir = get_project_root() / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    return sessions_dir


def get_exports_dir() -> Path:
    """获取导出目录"""
    exports_dir = get_project_root() / "exports"
    exports_dir.mkdir(exist_ok=True)
    return exports_dir


def generate_session_id() -> str:
    """生成会话ID（包含微秒避免同秒碰撞）"""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def extract_file_paths(text: str, validate_exists: bool = True) -> Set[str]:
    """从文本中提取文件路径

    Args:
        text: 要搜索的文本
        validate_exists: 是否验证文件存在（默认True，设为False可提高性能）

    Returns:
        匹配到的文件路径集合
    """
    paths = set()

    # 使用预编译的正则表达式匹配路径（Unix + Windows 绝对路径）
    paths.update(_RE_UNIX_ABS_PATH.findall(text))
    paths.update(_RE_WIN_ABS_PATH.findall(text))

    rel_matches = _RE_REL_PATH.findall(text)
    paths.update(rel_matches)

    # 如果不需要验证存在性，直接返回
    if not validate_exists:
        return paths

    # 过滤存在的文件
    valid_paths = set()
    cwd = Path.cwd()
    for p in paths:
        path = Path(p)
        try:
            if path.exists() and path.is_file():
                valid_paths.add(str(path.absolute()))
            elif not path.is_absolute():
                # 尝试在当前目录查找
                cwd_path = cwd / p
                if cwd_path.exists() and cwd_path.is_file():
                    valid_paths.add(str(cwd_path.absolute()))
        except (OSError, ValueError):
            # 忽略无效路径
            continue

    return valid_paths


def copy_files_to_export(files: List[str], export_dir: Path) -> dict:
    """
    复制文件到导出目录
    返回原路径到新路径的映射
    """
    assets_dir = export_dir / "assets"
    assets_dir.mkdir(exist_ok=True)

    mapping = {}
    for file_path in files:
        src = Path(file_path)
        if src.exists():
            # 避免文件名冲突
            dest_name = src.name
            dest = assets_dir / dest_name
            counter = 1
            while dest.exists():
                stem = src.stem
                suffix = src.suffix
                dest_name = f"{stem}_{counter}{suffix}"
                dest = assets_dir / dest_name
                counter += 1

            shutil.copy2(src, dest)
            mapping[file_path] = str(dest.relative_to(export_dir))

    return mapping


_RE_ANSI_STRIP = re.compile('|'.join([
    r'\x1b\[[0-9;?]*[a-zA-Z]',          # CSI序列 (包括 \x1b[?25h 等)
    r'\x1b\](?:[^\x07\x1b]|\x1b[^\\])*(?:\x07|\x1b\\)',  # OSC序列 (BEL 或 ST 终止)
    r'\x1b[PX^_].*?\x1b\\',             # 其他转义序列
    r'\x1b[()][AB012]',                  # 字符集选择
    r'\x1b[=>]',                         # 键盘模式
    r'\x1b\[[\d;]*[ q]',                # 光标样式
    r'\r',                               # 回车符（避免覆盖行）
]))
_RE_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def strip_ansi(text: str) -> str:
    """移除ANSI转义序列和终端控制字符"""
    result = _RE_ANSI_STRIP.sub('', text)
    result = _RE_CONTROL_CHARS.sub('', result)
    return result


def is_image_file(path: str) -> bool:
    """判断是否为图片文件"""
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def format_timestamp(dt: datetime = None) -> str:
    """格式化时间戳"""
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_file_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    for unit in ['KB', 'MB', 'GB']:
        size_bytes /= 1024
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
    return f"{size_bytes:.1f} TB"
