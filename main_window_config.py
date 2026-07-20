"""MainWindow 的配置读写混入（从 main_window.py 拆出）。

主配置的加载/构建/保存、LLM 配置查询、本地命令与本地配置目录。纯方法
搬迁，行为不变；对进程级共享类属性 _shared_left_panel_width /
_navigator_dock_mode / _global_window_navigator 的读写走延迟引用
_mw.MainWindow（见下方 import 注释），落在真正的 MainWindow 类上。
"""
import app_config
import os
import shutil
import sys
from PyQt6 import sip
from PyQt6.QtWidgets import QMessageBox
from datetime import datetime
from dialogs import get_default_shell
from i18n import get_language, set_language, t
from pathlib import Path
from terminal_widget import TerminalWidget
from utils import atomic_write_json

# 延迟引用宿主类：这些是 MainWindow 上的**进程级共享**类属性（跨窗口
# 单例：面板宽度、导航停靠方式、全局导航器），必须落在真正的 MainWindow
# 类上，而非 type(self)（后者对单元测试的假 self / 潜在子类会取错）。
# 顶层 import main_window 会与 main_window 顶层 import 本模块形成循环，但
# 只在方法内访问 .MainWindow（调用时两模块都已加载完），是既定安全模式。
import main_window as _mw
from app_logging import get_logger

logger = get_logger(__name__)


class ConfigMixin:

    def _load_config(self):
        """加载配置（预设命令等）"""
        # 迁移旧配置文件（从用户目录迁移到程序目录）
        old_config = Path.home() / ".smart_terminal_config.json"
        if not self.CONFIG_FILE.exists() and old_config.exists():
            shutil.copy2(old_config, self.CONFIG_FILE)

        self.last_preset_index = 0  # 默认选中第一个
        self.image_prefix_enabled = False  # 图片路径是否加@前缀
        self.image_save_local = True  # 图片是否保存到工作目录（默认开启，方便Gemini访问）
        # 是否把鼠标点击转发给开启鼠标上报的 TUI（Claude Code 选项菜单 / lazygit / fzf /
        # htop）。默认关闭，避免在 Claude Code 里误点选项。滚轮上报不受此开关影响。
        self._mouse_click_forward_enabled = False
        self.working_dir_history = []  # 工作目录历史
        self._working_dir_freq = {}  # 工作目录使用频率 {path: count}
        self._dir_history_removed = set()  # 用户显式删除的路径黑名单（随配置持久化，防止启动 cwd 自动加入/多窗口合并把它复活）
        self._dir_history_readded = set()  # 拉黑后又被用户显式选回的路径（保存时据此解除拉黑）
        self.last_working_dir = None  # 上次使用的工作目录
        self.toolbar_config = None  # 工具栏配置
        self.llm_configs = []  # LLM API 配置列表
        self.default_llm_config = 0  # 默认 LLM 配置索引
        self._ai_completion_enabled = False  # AI 行内补全开关（默认关闭）
        self._editor_word_wrap = False  # 编辑器自动换行开关（默认关闭）
        self._saved_window_geometry = None  # 窗口位置和大小 [x, y, w, h]
        self._saved_window_maximized = False  # 窗口是否最大化
        self._saved_explorer_panel_visible = False  # Explorer 面板可见性
        self._saved_git_panel_visible = False  # Git 面板可见性
        self._saved_log_panel_visible = False  # 日志面板可见性
        self._saved_navigator_enabled = True  # Window Navigator 开关状态（默认开启）
        # 记忆资源管理器/编辑器拖拽过的尺寸，避免每次重新打开都重置
        self._saved_explorer_main_sizes = None  # main_splitter 4 项尺寸（左右分屏）
        self._saved_explorer_internal_sizes = None  # explorer_splitter 2 项尺寸（上下分屏）
        # 弹簧模式：编辑器与终端左右并排时，点哪边哪边自动变宽，另一边收窄但不收起（默认开启）
        self._spring_mode_enabled = True
        self._spring_current_side = None   # 'editor' / 'terminal'，当前已展开的一侧
        self._spring_width_gate = True     # 窗口宽度是否允许 spring 生效（resize 时按滞回更新）
        self._applying_spring = False      # setSizes 期间置位，避免污染记忆尺寸
        self._spring_anim = None           # 进行中的尺寸过渡动画（持引用防 GC）
        self._saved_remote_internal_sizes = None  # remote_splitter 2 项尺寸（上下分屏）
        # 左侧栏宽度是进程级共享的（见 _shared_left_panel_width）：新窗口初始化时
        # 不要把已打开窗口设过的宽度清成 None，仅在还没有任何窗口设过时才置默认。
        if _mw.MainWindow._shared_left_panel_width is None:
            self._saved_left_panel_width = None  # 仅左面板可见时的宽度（无编辑器场景）
        self._saved_git_commit_height = None  # Git 面板提交区高度（拖拽记忆，兼容旧版）
        self._saved_git_body_sizes = None     # Git 面板 body splitter 各栏尺寸（拖拽记忆）
        self._saved_nav_list_height = None    # 内嵌导航列表高度（拖拽记忆）
        self._custom_shortcuts = {}           # 用户自定义快捷键覆盖 {action_id: seq}
        self.used_label_names = []            # 用过的 标签/分屏 名称历史（可复用）
        self._notify_sound = 'Submarine'      # 完成提示音（绿点点亮时播放；'' = 静音）
        try:
            config = app_config.read_config()
            if config:
                self.presets = config.get('presets', [])
                self.last_preset_index = config.get('last_preset_index', 0)
                self.image_prefix_enabled = config.get('image_prefix_enabled', False)
                self.image_save_local = config.get('image_save_local', True)
                self._mouse_click_forward_enabled = config.get('mouse_click_forward_enabled', False)
                self.used_label_names = config.get('used_label_names', [])
                self.working_dir_history = config.get('working_dir_history', [])
                self._working_dir_freq = config.get('working_dir_freq', {})
                # 用户显式删除的路径黑名单：过滤掉被其它窗口写回的黑名单路径
                self._dir_history_removed = set(config.get('working_dir_removed', []) or [])
                if self._dir_history_removed:
                    self.working_dir_history = [
                        p for p in self.working_dir_history
                        if p not in self._dir_history_removed]
                # 兼容旧配置：为没有频率记录的历史路径补默认值
                for p in self.working_dir_history:
                    if p not in self._working_dir_freq:
                        self._working_dir_freq[p] = 1
                # 按频率倒序排列
                self.working_dir_history.sort(key=lambda p: self._working_dir_freq.get(p, 0), reverse=True)
                self.last_working_dir = config.get('last_working_dir', None)
                # 加载主题设置
                saved_theme = config.get('theme', '午夜黑')
                if saved_theme in self.THEMES:
                    self.current_theme = saved_theme
                # 加载图标蒙版设置
                self._use_icon_tint = config.get('icon_tint', False)
                # 加载工具栏配置（并做一次性顺序重整：remote 紧跟 git、images 紧跟 clear）
                self.toolbar_config = config.get('toolbar_config', None)
                if self.toolbar_config:
                    from toolbar_manager import migrate_toolbar_order
                    migrate_toolbar_order(self.toolbar_config)
                # 加载 LLM 配置
                self.llm_configs = config.get('llm_configs', [])
                self.default_llm_config = config.get('default_llm_config', 0)
                # 加载全局缩放偏移
                self._global_zoom_delta = config.get('global_zoom_delta', 0)
                # 加载 GUI 字体大小
                self._gui_font_size = config.get('gui_font_size', 0)
                # 加载固定第二排工具栏设置
                self._pin_toolbar_row2 = config.get('pin_toolbar_row2', True)
                # 加载窗口透明度
                self._window_opacity = config.get('window_opacity', 100)
                # 加载左右分屏偏好（Explorer / Remote 各自记忆；Explorer 默认左右并排）
                self._explorer_split_horizontal = config.get('explorer_split_horizontal', True)
                self._remote_split_horizontal = config.get('remote_split_horizontal', False)
                # 加载弹簧模式偏好（默认开启；老配置里显式存过 false 则尊重用户选择）
                self._spring_mode_enabled = config.get('spring_mode_enabled', True)
                # 一次性迁移（2026-07）：左右分屏 + 弹簧改为默认开启，老配置里
                # 保存过 False 的也翻一次到新默认；标记落盘后不再重复，之后
                # 用户的手动开关照常记忆
                if not config.get('split_spring_default_on_migrated'):
                    self._explorer_split_horizontal = True
                    self._spring_mode_enabled = True
                    app_config.update_config(
                        {'split_spring_default_on_migrated': True,
                         'explorer_split_horizontal': True,
                         'spring_mode_enabled': True},
                        description='split/spring default-on migration')
                # 加载 AI 行内补全开关
                self._ai_completion_enabled = config.get('ai_completion_enabled', False)
                self._editor_word_wrap = config.get('editor_word_wrap', False)
                # 加载完成提示音（绿点点亮时播放；'' = 静音）
                self._notify_sound = config.get('notify_sound', 'Submarine')
                # 加载终端 scrollback 上限（进程级，影响之后新建的终端）
                TerminalWidget.SCROLLBACK_LINES = self._clamp_scrollback(
                    config.get('terminal_scrollback', TerminalWidget.SCROLLBACK_LINES))
                # 终端解析放到后台线程：env 变量优先（已在类属性默认里处理，
                # 可 =0 强制关 / =1 强制开），否则用配置值。
                # 键名从 parse_off_gui_thread 换成 parse_on_reader_thread：
                # 旧键被 _save_config 无条件写入过，老配置里存着旧默认 false，
                # 沿用旧键会把新默认压回关闭。旧键值不迁移（false 是旧默认而非
                # 用户选择，无法区分），统一按新默认 True 起步，菜单仍可关。
                if os.environ.get('STELLAR_PARSE_OFF_GUI') is None:
                    TerminalWidget.PARSE_ON_READER_THREAD = bool(
                        config.get('parse_on_reader_thread', True))
                # 加载导航面板停靠方式（'float' / 'embed'，全局记忆）
                _dock_mode = config.get('navigator_dock_mode', 'embed')
                if _dock_mode in ('float', 'embed'):
                    _mw.MainWindow._navigator_dock_mode = _dock_mode
                # 加载用户自定义快捷键覆盖
                ks = config.get('keyboard_shortcuts', {})
                if isinstance(ks, dict):
                    self._custom_shortcuts = {
                        str(k): str(v) for k, v in ks.items() if isinstance(v, str)
                    }
                # 加载语言设置
                saved_lang = config.get('language', 'zh')
                if saved_lang in ('zh', 'en'):
                    set_language(saved_lang)
                # 加载窗口几何与面板可见性
                self._saved_window_geometry = config.get('window_geometry', None)
                self._saved_window_maximized = config.get('window_maximized', False)
                self._saved_explorer_panel_visible = config.get('explorer_panel_visible', False)
                self._saved_git_panel_visible = config.get('git_panel_visible', False)
                self._saved_log_panel_visible = config.get('log_panel_visible', False)
                self._saved_navigator_enabled = config.get('navigator_enabled', True)
                # 加载记忆的资源管理器/编辑器尺寸
                main_sizes = config.get('explorer_main_splitter_sizes', None)
                if isinstance(main_sizes, list) and len(main_sizes) == 4 and all(isinstance(s, int) and s >= 0 for s in main_sizes):
                    self._saved_explorer_main_sizes = main_sizes
                internal_sizes = config.get('explorer_internal_splitter_sizes', None)
                if isinstance(internal_sizes, list) and len(internal_sizes) == 2 and all(isinstance(s, int) and s >= 0 for s in internal_sizes):
                    self._saved_explorer_internal_sizes = internal_sizes
                remote_internal = config.get('remote_internal_splitter_sizes', None)
                if isinstance(remote_internal, list) and len(remote_internal) == 2 and all(isinstance(s, int) and s >= 0 for s in remote_internal):
                    self._saved_remote_internal_sizes = remote_internal
                # 左侧栏宽度是进程级共享的：只让第一个窗口从磁盘播种，之后开的
                # 窗口沿用已有的实时共享值，避免用磁盘上的旧值覆盖别的窗口刚拖出的新宽度。
                left_width = config.get('left_panel_width', None)
                if (isinstance(left_width, int) and left_width > 0
                        and _mw.MainWindow._shared_left_panel_width is None):
                    self._saved_left_panel_width = left_width
                git_commit_h = config.get('git_commit_height', None)
                if isinstance(git_commit_h, int) and git_commit_h > 0:
                    self._saved_git_commit_height = git_commit_h
                git_body_sizes = config.get('git_body_splitter_sizes', None)
                if isinstance(git_body_sizes, list) and git_body_sizes and all(isinstance(s, int) and s >= 0 for s in git_body_sizes):
                    self._saved_git_body_sizes = git_body_sizes
                nav_list_h = config.get('nav_list_height', None)
                if isinstance(nav_list_h, int) and nav_list_h > 0:
                    self._saved_nav_list_height = nav_list_h
        except Exception:
            self.presets = []

        # 确保当前目录在历史中。
        # 例外：文件系统根目录（mac 打包 app 从 Dock/Finder/升级脚本启动时
        # cwd 就是 "/"，Windows 下可能是盘符根）不是用户选过的目录，不自动
        # 加入；历史里已被旧版本自动加入的根目录也顺手登记为待删——否则
        # 用户删掉 "/" 后每次启动都被加回来，永远删不干净。
        def _is_fs_root(p):
            ap = os.path.abspath(p or os.sep)
            return os.path.dirname(ap) == ap

        stale_roots = {p for p in self.working_dir_history if _is_fs_root(p)}
        if stale_roots:
            self.working_dir_history = [
                p for p in self.working_dir_history if p not in stale_roots]
            # 记入持久黑名单：下次保存与磁盘并集时把根目录从共享配置剔除
            self._dir_history_removed |= stale_roots
        # 用户显式删除过的路径同理不自动加回——Linux 从桌面/启动器启动时
        # cwd 是 $HOME（如 /home/zy），不挡这一步删掉的家目录每次启动都会复活。
        current_dir = os.getcwd()
        if not _is_fs_root(current_dir) and current_dir not in self._dir_history_removed:
            if current_dir not in self.working_dir_history:
                self.working_dir_history.append(current_dir)
            if current_dir not in self._working_dir_freq:
                self._working_dir_freq[current_dir] = 1
        # 按频率倒序排列
        self.working_dir_history.sort(key=lambda p: self._working_dir_freq.get(p, 0), reverse=True)

        # 确保有默认预设
        if not self.presets:
            default_shell = get_default_shell()

            # Windows 使用 set，Unix 使用 export 设置环境变量
            def _proxy_cmds(port):
                prefix = 'set' if sys.platform == 'win32' else 'export'
                return [
                    f'{prefix} http_proxy=http://127.0.0.1:{port}/',
                    f'{prefix} https_proxy=http://127.0.0.1:{port}/'
                ]

            self.presets = [
                {
                    'name': default_shell,
                    'commands': [default_shell]
                },
                {
                    'name': 'Claude Fable (with proxy)',
                    'commands': [default_shell] + _proxy_cmds(7897) + ['claude --model fable']
                },
                {
                    'name': 'Claude Opus (with proxy)',
                    'commands': [default_shell] + _proxy_cmds(1081) + ['claude --model opus']
                },
                {
                    'name': 'Claude Sonnet',
                    'commands': [
                        default_shell,
                        'claude --model sonnet'
                    ]
                }
            ]

        # 确保有默认 LLM 配置
        if not self.llm_configs:
            self.llm_configs = [
                {
                    'name': 'OpenAI GPT-4',
                    'api_base': 'https://api.openai.com/v1',
                    'api_key': '',
                    'model': 'gpt-4',
                    'timeout': 30,
                    'max_tokens': 4096,
                    'temperature': 1.0,
                    'top_p': 1.0,
                    'proxy': ''
                }
            ]
            self.default_llm_config = 0

    def _save_config(self):
        """保存配置（app_config 单点：进程间文件锁 + 原子写 + 失败可见）"""
        try:
            # 先把磁盘上其它窗口新增的目录历史并入本窗口，避免后写覆盖先写
            self._merge_dir_history_for_save()
            # 读-合-写全程在 app_config 的锁内执行；文件损坏时放弃本次保存
            # （宁可不存，也不能把其它进程维护的字段当"损坏"覆盖掉）
            ok = app_config.update_config_with(
                self._build_config_for_save, description='main-window')
            if not ok:
                # 写失败不再无声丢失（磁盘满/权限/文件损坏），状态栏提示
                try:
                    self.statusbar.showMessage(t("status.config_save_failed"), 5000)
                except Exception:
                    logger.debug("_save_config: suppressed exception", exc_info=True)
        except Exception:
            logger.debug("_save_config: suppressed exception", exc_info=True)

    def _build_config_for_save(self, existing_config: dict):
        """在 app_config 锁内执行：把本窗口的设置原地合并进 existing_config。

        existing_config 是磁盘上的现有配置：既用于"未修改预设时回退到磁盘
        版本"，又把本函数没列出的字段（如 git_widget 写入的 git_proxy /
        git_proxies）原样保留下来。
        """
        if True:  # 与旧 try 块保持同缩进层级，减小 diff
            # 获取当前选中的预设索引
            current_index = self.preset_combo.currentIndex() if hasattr(self, 'preset_combo') else 0
            image_prefix = self.image_prefix_checkbox.isChecked() if hasattr(self, 'image_prefix_checkbox') else False
            image_local = self.image_local_checkbox.isChecked() if hasattr(self, 'image_local_checkbox') else True
            # 限制历史记录数量
            dir_history = self.working_dir_history if hasattr(self, 'working_dir_history') else []
            # 使用窗口级别的工作目录
            last_cwd = self._window_cwd if hasattr(self, '_window_cwd') else os.getcwd()

            # 防止多窗口覆盖：如果本窗口没有修改预设，从磁盘加载最新的预设
            # 这样关闭窗口时不会覆盖其他窗口保存的预设
            if getattr(self, '_presets_modified', False):
                presets_to_save = self.presets
            else:
                presets_to_save = existing_config.get('presets', self.presets)

            # 同理保护 LLM API 配置：本窗口没改过就用磁盘上的最新值，避免一个持有
            # 旧副本的窗口（不是最后关闭的那个）在退出时把别的窗口新存的 API 配置覆盖掉。
            if getattr(self, '_llm_configs_modified', False):
                llm_configs_to_save = self.llm_configs
                default_llm_to_save = self.default_llm_config
            else:
                llm_configs_to_save = existing_config.get('llm_configs', self.llm_configs)
                default_llm_to_save = existing_config.get('default_llm_config', self.default_llm_config)

            # 同理保护自定义快捷键：本窗口没改过就用磁盘最新值，避免覆盖其它窗口的改动
            if getattr(self, '_shortcuts_modified', False):
                shortcuts_to_save = getattr(self, '_custom_shortcuts', {})
            else:
                shortcuts_to_save = existing_config.get('keyboard_shortcuts',
                                                        getattr(self, '_custom_shortcuts', {}))

            config = {
                'presets': presets_to_save,
                'last_preset_index': current_index,
                'image_prefix_enabled': image_prefix,
                'image_save_local': image_local,
                'mouse_click_forward_enabled': getattr(self, '_mouse_click_forward_enabled', False),
                'working_dir_history': dir_history,
                'used_label_names': self._merged_label_names(existing_config),
                'working_dir_freq': self._working_dir_freq if hasattr(self, '_working_dir_freq') else {},
                'working_dir_removed': sorted(getattr(self, '_dir_history_removed', None) or set()),
                'last_working_dir': last_cwd,
                'theme': self.current_theme,  # 保存主题设置
                'icon_tint': self._use_icon_tint,  # 保存图标蒙版设置
                'toolbar_config': self.toolbar_config,  # 保存工具栏配置
                'llm_configs': llm_configs_to_save,  # 保存 LLM 配置（带多窗口防覆盖）
                'default_llm_config': default_llm_to_save,  # 保存默认 LLM 配置索引
                'global_zoom_delta': self._global_zoom_delta,  # 保存全局缩放偏移
                'gui_font_size': self._gui_font_size,  # 保存 GUI 字体大小
                'pin_toolbar_row2': self._pin_toolbar_row2,  # 保存固定第二排工具栏
                'window_opacity': self._window_opacity,  # 保存窗口透明度
                'explorer_split_horizontal': getattr(self, '_explorer_split_horizontal', True),  # 保存左右分屏偏好
                'remote_split_horizontal': getattr(self, '_remote_split_horizontal', False),  # Remote 左右分屏偏好
                'spring_mode_enabled': getattr(self, '_spring_mode_enabled', False),  # 保存弹簧模式偏好
                'ai_completion_enabled': getattr(self, '_ai_completion_enabled', False),  # 保存 AI 行内补全开关
                'editor_word_wrap': getattr(self, '_editor_word_wrap', False),  # 保存编辑器自动换行开关
                'notify_sound': getattr(self, '_notify_sound', 'Submarine'),  # 保存完成提示音
                'terminal_scrollback': TerminalWidget.SCROLLBACK_LINES,  # 保存终端 scrollback 上限
                'parse_on_reader_thread': TerminalWidget.PARSE_ON_READER_THREAD,  # 保存"解析放后台线程"开关（旧键 parse_off_gui_thread 已废弃）
                'language': get_language(),  # 保存语言设置
                'keyboard_shortcuts': shortcuts_to_save,  # 保存自定义快捷键（带多窗口防覆盖）
                'window_geometry': [self.x(), self.y(), self.width(), self.height()],
                'window_maximized': self.isMaximized(),
                'explorer_panel_visible': getattr(self, 'explorer_panel_visible', False),
                'git_panel_visible': getattr(self, 'git_panel_visible', False),
                'log_panel_visible': getattr(self, 'log_panel_visible', False),
                'navigator_enabled': self._navigator_is_enabled(),  # 记忆 Window Navigator 开关状态

                'explorer_main_splitter_sizes': getattr(self, '_saved_explorer_main_sizes', None),
                'explorer_internal_splitter_sizes': getattr(self, '_saved_explorer_internal_sizes', None),
                'remote_internal_splitter_sizes': getattr(self, '_saved_remote_internal_sizes', None),
                'left_panel_width': getattr(self, '_saved_left_panel_width', None),
                'git_commit_height': getattr(self, '_saved_git_commit_height', None),
                'git_body_splitter_sizes': getattr(self, '_saved_git_body_sizes', None),
                'nav_list_height': getattr(self, '_saved_nav_list_height', None),
            }
            # 保存窗口导航面板设置
            nav = _mw.MainWindow._global_window_navigator
            if nav is not None and not sip.isdeleted(nav):
                config['navigator_geometry'] = [nav.x(), nav.y(), nav.width(), nav.height()]
                config['navigator_font_size'] = nav._font_size
            else:
                # 导航面板已关闭，保留之前已落盘的几何 / 字号
                if 'navigator_geometry' in existing_config:
                    config['navigator_geometry'] = existing_config['navigator_geometry']
                if 'navigator_font_size' in existing_config:
                    config['navigator_font_size'] = existing_config['navigator_font_size']
            # 原地合并：保留由其它组件维护、本函数未列出的字段（如 git_widget
            # 写入的 git_proxy / git_proxies）。写盘由 app_config 在锁内原子完成。
            existing_config.update(config)
            # 清理已废弃的旧键（解析开关换键名后，旧键会因合并写永久残留）
            existing_config.pop('parse_off_gui_thread', None)

    def get_llm_config(self, name: str = None) -> dict:
        """获取指定名称的 LLM 配置，若不指定则返回默认配置

        Args:
            name: 配置名称，若为 None 则返回默认配置

        Returns:
            LLM 配置字典，若未找到则返回 None
        """
        if not self.llm_configs:
            return None

        if name is None:
            # 返回默认配置
            if 0 <= self.default_llm_config < len(self.llm_configs):
                return self.llm_configs[self.default_llm_config].copy()
            return self.llm_configs[0].copy() if self.llm_configs else None

        # 按名称查找
        for config in self.llm_configs:
            if config.get('name') == name:
                return config.copy()
        return None

    def get_completion_llm_config(self) -> dict:
        """AI 行内补全用的 LLM 配置。

        优先级：① 在 ✨ 里用「设为补全模型」显式指派(for_completion) →
        ② 兼容旧约定：名字叫 completion/补全 等 → ③ 回退默认配置。
        """
        if self.llm_configs:
            for config in self.llm_configs:
                if config.get('for_completion'):
                    return config.copy()
            for config in self.llm_configs:
                if (config.get('name') or '').strip().lower() in self._COMPLETION_CONFIG_NAMES:
                    return config.copy()
        return self.get_llm_config()

    def get_git_llm_config(self) -> dict:
        """Git 提交信息生成用的 LLM 配置：
        优先用「设为 Git 模型」显式指派(for_git)，否则回退默认配置。"""
        if self.llm_configs:
            for config in self.llm_configs:
                if config.get('for_git'):
                    return config.copy()
        return self.get_llm_config()

    def get_all_llm_configs(self) -> list:
        """获取所有 LLM 配置列表

        Returns:
            LLM 配置列表的副本
        """
        return [c.copy() for c in self.llm_configs]

    def _ensure_local_config_dir(self) -> bool:
        """确保 .sterminal 目录存在

        Returns:
            bool: 成功创建或已存在返回 True，失败返回 False
        """
        # 检查是否为特殊目录（禁止创建配置）
        forbidden_dirs = ['/', '/usr', '/bin', '/sbin', '/etc', '/var', '/tmp', '/private']
        if self._window_cwd in forbidden_dirs:
            self.statusbar.showMessage(t("status.cannot_create_config", cwd=self._window_cwd), 3000)
            return False

        config_dir = Path(self._window_cwd) / self.LOCAL_CONFIG_DIR
        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            return True
        except PermissionError:
            self._styled_message_box(
                QMessageBox.Icon.Warning,
                t("msg.permission_denied"),
                t("msg.cannot_create_config_dir", cwd=self._window_cwd)
            )
            return False
        except Exception as e:
            self.statusbar.showMessage(t("status.config_dir_error", error=str(e)), 3000)
            return False

    def _save_local_commands(self):
        """保存本地命令配置"""
        if not self._ensure_local_config_dir():
            return False

        config_path = self._get_local_commands_path()
        data = {
            "version": 1,
            "presets": self.local_presets,
            "updated_at": datetime.now().isoformat()
        }

        try:
            # 原子写：直写会先 truncate，写一半崩溃/磁盘满会留下半截 JSON
            if not atomic_write_json(config_path, data):
                raise OSError(f"write failed: {config_path}")
            return True
        except PermissionError:
            self._styled_message_box(
                QMessageBox.Icon.Warning,
                t("msg.save_failed"),
                t("msg.cannot_write_config", path=config_path)
            )
            return False
        except Exception as e:
            self.statusbar.showMessage(t("status.save_local_commands_error", error=str(e)), 3000)
            return False
