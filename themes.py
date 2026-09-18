"""主题配色表（从 main_window.py 拆出的纯数据）。

每个主题一套配色键（背景/前景/强调色/终端色板等）。MainWindow 以
`THEMES = themes.THEMES` 作为类属性引用本表，`self.THEMES[...]` 访问不变。
应用主题的逻辑（_apply_theme 等）仍在 main_window，因为它耦合大量 widget。
"""

THEMES = {
    "深蓝": {
        "name": "深蓝",
        "bg_darkest": "#0f0f1a",
        "bg_dark": "#1a1a2e",
        "bg_medium": "#16213e",
        "bg_light": "#2d2d44",
        "bg_lighter": "#3d3d5c",
        "bg_hover": "#4d4d6c",
        "accent": "#667eea",
        "accent_hover": "#7a8efa",
        "accent_pressed": "#5a6fd6",
        "text": "#eaeaea",
        "text_dim": "#888888",
        "border": "#3d3d5c",
        "success": "#4ade80",
        "success_hover": "#22c55e",
        "danger": "#ef4444",
        "danger_hover": "#dc2626",
        "terminal_bg": "#282c34",
        "terminal_fg": "#abb2bf",
    },
    "暗夜紫": {
        "name": "暗夜紫",
        "bg_darkest": "#13111a",
        "bg_dark": "#1e1a2e",
        "bg_medium": "#2a2440",
        "bg_light": "#3a3350",
        "bg_lighter": "#4a4360",
        "bg_hover": "#5a5370",
        "accent": "#a855f7",
        "accent_hover": "#c084fc",
        "accent_pressed": "#9333ea",
        "text": "#f0e6ff",
        "text_dim": "#9988aa",
        "border": "#4a4360",
        "success": "#4ade80",
        "success_hover": "#22c55e",
        "danger": "#f43f5e",
        "danger_hover": "#e11d48",
        "terminal_bg": "#1a1625",
        "terminal_fg": "#e0d6f0",
    },
    "森林绿": {
        "name": "森林绿",
        "bg_darkest": "#0a1410",
        "bg_dark": "#12201a",
        "bg_medium": "#1a3025",
        "bg_light": "#254035",
        "bg_lighter": "#305545",
        "bg_hover": "#406555",
        "accent": "#22c55e",
        "accent_hover": "#4ade80",
        "accent_pressed": "#16a34a",
        "text": "#e6f4ea",
        "text_dim": "#88aa99",
        "border": "#305545",
        "success": "#4ade80",
        "success_hover": "#86efac",
        "danger": "#ef4444",
        "danger_hover": "#dc2626",
        "terminal_bg": "#0f1a14",
        "terminal_fg": "#c8e6d0",
    },
    "暖橙": {
        "name": "暖橙",
        "bg_darkest": "#1a1008",
        "bg_dark": "#2a1a10",
        "bg_medium": "#3a2818",
        "bg_light": "#4a3828",
        "bg_lighter": "#5a4838",
        "bg_hover": "#6a5848",
        "accent": "#f97316",
        "accent_hover": "#fb923c",
        "accent_pressed": "#ea580c",
        "text": "#fff4e6",
        "text_dim": "#aa9988",
        "border": "#5a4838",
        "success": "#84cc16",
        "success_hover": "#a3e635",
        "danger": "#ef4444",
        "danger_hover": "#dc2626",
        "terminal_bg": "#1f1610",
        "terminal_fg": "#f0e0d0",
    },
    "午夜黑": {
        "name": "午夜黑",
        "bg_darkest": "#000000",
        "bg_dark": "#0a0a0a",
        "bg_medium": "#141414",
        "bg_light": "#1e1e1e",
        "bg_lighter": "#2a2a2a",
        "bg_hover": "#3a3a3a",
        "accent": "#3b82f6",
        "accent_hover": "#60a5fa",
        "accent_pressed": "#2563eb",
        "text": "#f5f5f5",
        "text_dim": "#888888",
        "border": "#2a2a2a",
        "success": "#22c55e",
        "success_hover": "#4ade80",
        "danger": "#ef4444",
        "danger_hover": "#f87171",
        "terminal_bg": "#000000",
        "terminal_fg": "#e0e0e0",
    },
    "浅色": {
        "name": "浅色",
        # macOS 风格浅色：近白的层级面板 + 中性灰按钮 + 单一蓝色强调色。
        # 深色主题里 darkest→hover 是"由暗到亮"的层级；浅色反过来，
        # darkest 是最灰的窗口底、medium 是纯白的输入区/列表。
        "bg_darkest": "#ececef",      # 窗口底 / 标签条 / 日志区
        "bg_dark": "#f5f5f7",         # 工具栏 / 面板 / 标签页 pane / 消息框
        "bg_medium": "#ffffff",       # 输入框 / 列表 / 状态栏 / 面板标题栏
        "bg_light": "#e3e3e8",        # 按下态 / 标签 hover / 分隔
        "bg_lighter": "#e8e8ed",      # 中性按钮底
        "bg_hover": "#dcdce2",        # 中性按钮 hover
        "accent": "#007aff",          # macOS 系统蓝
        "accent_hover": "#2b8cff",
        "accent_pressed": "#0062cc",
        "text": "#1d1d1f",            # 主文字（Apple 标准深灰）
        "text_dim": "#6e6e73",        # 次要文字
        "border": "#d1d1d6",          # 发丝线边框
        "success": "#2ea043",         # 白字可读的绿
        "success_hover": "#3fb950",
        "danger": "#e0383e",
        "danger_hover": "#f0555b",
        "terminal_bg": "#ffffff",
        "terminal_fg": "#1d1d1f",
        "is_light_theme": True,  # 标记这是浅色主题
        # 浅色主题专用的 ANSI 终端颜色（深色文字）
        "terminal_colors": {
            "black": "#1d1d1f",
            "red": "#c41a16",
            "green": "#007400",
            "brown": "#a85400",
            "yellow": "#a85400",
            "blue": "#0451a5",
            "magenta": "#bc05bc",
            "cyan": "#0598bc",
            "white": "#6e6e73",
            "default": "#1d1d1f",
        },
        "terminal_bright_colors": {
            "black": "#4e4e52",
            "red": "#de3124",
            "green": "#00a800",
            "brown": "#cc6600",
            "yellow": "#cc6600",
            "blue": "#2f86d2",
            "magenta": "#d416d4",
            "cyan": "#00a8a8",
            "white": "#3e3e42",
        },
        "selection_color": (0, 122, 255, 55),  # 系统蓝选区
        "cursor_color": (29, 29, 31, 200),  # 深色光标
    },
    "粉红": {
        "name": "粉红",
        "bg_darkest": "#1a0a14",
        "bg_dark": "#2e1a28",
        "bg_medium": "#3e2838",
        "bg_light": "#4e3848",
        "bg_lighter": "#5e4858",
        "bg_hover": "#6e5868",
        "accent": "#ec4899",
        "accent_hover": "#f472b6",
        "accent_pressed": "#db2777",
        "text": "#fce7f3",
        "text_dim": "#aa8899",
        "border": "#5e4858",
        "success": "#4ade80",
        "success_hover": "#22c55e",
        "danger": "#ef4444",
        "danger_hover": "#dc2626",
        "terminal_bg": "#1f1018",
        "terminal_fg": "#f0d8e8",
    },
}


def is_light(theme: dict) -> bool:
    """该主题是否为浅色主题（浅色下品牌色按钮/彩色文字要走中性化处理）。"""
    return bool(theme.get('is_light_theme'))


def _rel_luminance(r: float, g: float, b: float) -> float:
    """WCAG 相对亮度（分量为 0～1）。"""
    def ch(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def readable_on_light(color_hex: str, min_contrast: float = 4.5) -> str:
    """把窗口色这类"为深色背景挑的亮色"压暗到白底上可读（对比度 ≥ 4.5:1）。

    窗口色（#667eea/#22c55e/#facc15…）是按深色底选的高亮度色，直接当浅色
    主题里的文字色对比度只有 2～3:1（导航列表里的绿字/黄字几乎看不见）。
    这里只逐级压 HSL 亮度、不动色相，窗口之间仍靠颜色区分；黄色这类本身
    亮度就高的色相会被压得更狠。非法输入原样返回。
    """
    import colorsys
    s = str(color_hex or '').strip()
    if len(s) != 7 or not s.startswith('#'):
        return color_hex
    try:
        r, g, b = (int(s[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    except ValueError:
        return color_hex
    if (1.05 / (_rel_luminance(r, g, b) + 0.05)) >= min_contrast:
        return s.lower()
    h, l, sat = colorsys.rgb_to_hls(r, g, b)
    while l > 0.02:
        l -= 0.02
        r, g, b = colorsys.hls_to_rgb(h, l, sat)
        if (1.05 / (_rel_luminance(r, g, b) + 0.05)) >= min_contrast:
            break
    return '#%02x%02x%02x' % (round(r * 255), round(g * 255), round(b * 255))
