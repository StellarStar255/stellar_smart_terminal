"""
工具函数模块
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Set

# 支持的文件扩展名
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.svg', '.ico'}
DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.csv'}
CODE_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.json', '.yaml', '.yml', '.md'}

ALL_EXTENSIONS = IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS | CODE_EXTENSIONS


def parse_search_tokens(query: str) -> list:
    """把搜索框文本拆成空格分隔的关键词（小写、去空白、去重保序）。

    多个关键词是 AND 关系：例如 "stellar terminal" 拆成 ['stellar', 'terminal']，
    可命中 "stellar_smart_terminal"。返回空列表表示「不过滤」。
    """
    seen = set()
    tokens = []
    for tok in query.lower().split():
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    return tokens


def name_matches_tokens(name: str, tokens: list) -> bool:
    """文件名是否命中全部关键词（大小写不敏感的子串 AND 匹配）。"""
    if not tokens:
        return True
    low = name.lower()
    return all(tok in low for tok in tokens)

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


def is_frozen() -> bool:
    """是否运行在 PyInstaller 等打包产物中"""
    return bool(getattr(sys, 'frozen', False))


def get_data_dir() -> Path:
    """用户数据目录（配置 / 会话 / 导出等的统一根目录）。

    - 源码运行：返回项目目录（Path(__file__).parent），与历史行为完全一致，
      现有用户数据仍在原位被找到。
    - 打包运行（frozen）：返回并创建平台标准的用户数据目录，避免把数据写进
      .app bundle 内部（/Applications 下无写权限，且 App Translocation 会
      让 bundle 路径每次启动都变化）。
    """
    if not is_frozen():
        return Path(__file__).parent
    if sys.platform == 'darwin':
        data_dir = Path.home() / "Library" / "Application Support" / "StellarSmartTerminal"
    elif sys.platform == 'win32':
        appdata = os.environ.get('APPDATA')
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        data_dir = base / "StellarSmartTerminal"
    else:
        data_dir = Path.home() / ".local" / "share" / "StellarSmartTerminal"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_config_path() -> Path:
    """主配置文件路径（.smart_terminal_config.json）"""
    return get_data_dir() / ".smart_terminal_config.json"


def get_sessions_dir() -> Path:
    """获取会话存储目录"""
    sessions_dir = get_data_dir() / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    return sessions_dir


def get_exports_dir() -> Path:
    """获取导出目录"""
    exports_dir = get_data_dir() / "exports"
    exports_dir.mkdir(exist_ok=True)
    return exports_dir


_last_session_id = None


def generate_session_id() -> str:
    """生成会话ID（含微秒，并保证单进程内严格递增唯一）。

    Windows 的时钟粒度约 1ms+，连续两次调用可能拿到完全相同的时间戳
    （CI 实测碰撞）；ID 是定宽零填充格式，字符串比较即时间序，碰撞时把
    上一个 ID 的微秒段 +1。微秒段用尽（同一刻 100 万次调用）时等下一个时钟刻。
    """
    global _last_session_id
    sid = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    while _last_session_id is not None and sid <= _last_session_id:
        head, us = _last_session_id.rsplit("_", 1)
        if int(us) < 999999:
            sid = f"{head}_{int(us) + 1:06d}"
        else:
            sid = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    _last_session_id = sid
    return sid


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
    return f"{size_bytes / 1024:.1f} TB"


def read_config_json(file_path: Path):
    """读取共享 JSON 配置：返回 (data_dict, ok)。

    - 文件不存在：返回 ({}, True)（视为新装环境，调用方可放心写入）。
    - 文件存在但解析失败 / 读取出错：返回 ({}, False)。
      调用方拿到 ok=False 时应当 **放弃本次保存**，避免把别的进程刚写到一半的
      配置当作"已损坏"全部丢弃 —— 这正是多窗口下 git_proxy / presets 莫名
      被清空的根因。
    """
    try:
        if not file_path.exists():
            return {}, True
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return (data or {}), True
    except (OSError, json.JSONDecodeError, ValueError):
        return {}, False


def atomic_write_json(file_path: Path, data) -> bool:
    """原子写入 JSON 配置文件。

    背景：多个 Smart Terminal 窗口（多进程）共用同一份配置文件。直接
    `with open(... 'w')` 会先 truncate 再逐步写入，期间另一个进程读到的就是
    被截断的半截 JSON → 解析失败 → 该进程把它当作"文件损坏"，进而以自己
    内存里残缺的配置覆盖回磁盘，造成 git_proxy / git_proxies 等"由其它组件
    维护、本进程不感知"的字段被清空。

    用 tempfile.mkstemp + Path.replace 的"先写临时文件，再原子改名"，
    其它进程在任意时刻读到的要么是旧的完整文件、要么是新的完整文件，
    不存在半截窗口。
    """
    try:
        directory = file_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(directory), suffix='.tmp', prefix='.config_'
        )
        try:
            with open(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            Path(tmp_path).replace(file_path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        # 配置可能含明文密钥（LLM 代理 api_key 等），强制仅属主可读写，
        # 防止旧文件残留的宽松权限或宽 umask 导致同机其它用户读取。
        try:
            file_path.chmod(0o600)
        except OSError:
            pass
        return True
    except OSError:
        # 临时文件创建/重命名失败时回退到直写，至少保证当前进程的设置能落盘
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                file_path.chmod(0o600)
            except OSError:
                pass
            return True
        except OSError:
            return False


# ===== 完成提示音（绿点点亮时播放，可在设置里选） =====
# macOS 走系统自带音效（/System/Library/Sounds/*.aiff），用 afplay 播放最稳；
# 其它平台暂不枚举系统音，list_notify_sounds 返回空 → 设置里只有「无」。

_MAC_SOUND_DIRS = (
    Path("/System/Library/Sounds"),
    Path.home() / "Library" / "Sounds",
)


def list_notify_sounds() -> List[str]:
    """返回可选的提示音名称列表（不含扩展名，按字母序）。

    仅在 macOS 上枚举系统/用户音效目录里的 .aiff；其它平台返回空列表。
    """
    if sys.platform != "darwin":
        return []
    names = set()
    for d in _MAC_SOUND_DIRS:
        try:
            for f in d.glob("*.aiff"):
                names.add(f.stem)
        except OSError:
            pass
    return sorted(names)


def _resolve_notify_sound_path(name: str):
    for d in _MAC_SOUND_DIRS:
        p = d / f"{name}.aiff"
        if p.exists():
            return p
    return None


def play_notify_sound(name: str) -> None:
    """异步播放指定提示音；name 为空或找不到文件时静默返回。

    非阻塞：用 Popen 启动 afplay 后立即返回，绝不卡 GUI 线程。
    """
    if not name or sys.platform != "darwin":
        return
    path = _resolve_notify_sound_path(name)
    if path is None:
        return
    try:
        subprocess.Popen(
            ["/usr/bin/afplay", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# ---------- 全局勾选框对勾图标 ----------

# 白色对勾，viewBox 16：QSS 的 image: 不缩放位图，SVG 在任意 DPR 下都清晰
_CHECKMARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="#ffffff" '
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_checkmark_url_cache = None


def checkbox_checkmark_url() -> str:
    """勾选框白色对勾 SVG 的路径（供 QSS `image: url(...)` 引用）。

    QSS 的 image 只认文件路径，故惰性写入临时目录一次并缓存。
    返回正斜杠路径（Windows 的 QSS url 同样要求 /）；写盘失败返回 ''，
    调用方应跳过 image 规则（回退为无对勾的实心块，不影响功能）。
    """
    global _checkmark_url_cache
    if _checkmark_url_cache is None:
        d = Path(tempfile.gettempdir()) / 'smart_terminal_assets'
        p = d / 'checkmark.svg'
        try:
            d.mkdir(exist_ok=True)
            p.write_text(_CHECKMARK_SVG, encoding='utf-8')
        except OSError:
            return ''
        _checkmark_url_cache = str(p).replace('\\', '/')
    return _checkmark_url_cache
