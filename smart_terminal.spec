# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Stellar Smart Terminal.

- onedir 模式（终端类应用，启动速度优先）
- macOS 下额外生成 .app bundle（图标 assets/smart_terminal.icns，由 build.sh 从 png 生成）
- 代码用 Path(__file__).parent / "assets" 定位资源，因此 datas 保持 "assets" 相对布局，
  PyInstaller 会把它放进 _internal/（与打包后模块 __file__ 同级），路径解析不变。
"""
import sys
from pathlib import Path

PROJ = Path(SPECPATH)  # noqa: F821 — SPECPATH 由 PyInstaller 注入
APP_DISPLAY_NAME = "Stellar Smart Terminal"
APP_NAME = "StellarSmartTerminal"

# ---------------------------------------------------------------------------
# 资源文件：保持与源码运行时相同的相对布局（<root>/assets/...）
# ---------------------------------------------------------------------------
datas = [
    (str(PROJ / "assets"), "assets"),
    # 版本号单一来源：app_updater.get_current_version 在 Windows/Linux
    # 冻结版读随包的 pyproject.toml（mac 读 Info.plist）
    (str(PROJ / "pyproject.toml"), "."),
]

# ---------------------------------------------------------------------------
# 图标（按平台选择；macOS 的 .icns 由 build.sh 生成，缺失时退化为无图标）
# ---------------------------------------------------------------------------
icon_file = None
if sys.platform == "darwin":
    _icns = PROJ / "assets" / "smart_terminal.icns"
    if _icns.exists():
        icon_file = str(_icns)
elif sys.platform == "win32":
    _ico = PROJ / "assets" / "smart_termina128.ico"
    if _ico.exists():
        icon_file = str(_ico)

a = Analysis(
    ["app.py"],
    pathex=[str(PROJ)],
    binaries=[],
    datas=datas,
    # PyQt6 / pyte / paramiko 能被自动发现；requests 与 QtNetwork（单实例
    # IPC 用，仅函数内延迟导入）显式声明保险
    hiddenimports=["requests", "PyQt6.QtNetwork"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 明显用不到的大件
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PIL",
        "IPython",
        "pytest",
        # 不需要的 Qt 大模块（项目只用 QtCore/QtGui/QtWidgets）
        "PyQt6.QtWebEngineCore",
        "PyQt6.QtWebEngineWidgets",
        "PyQt6.QtQml",
        "PyQt6.QtQuick",
        "PyQt6.QtMultimedia",
        "PyQt6.Qt3DCore",
        "PyQt6.QtCharts",
        "PyQt6.QtDataVisualization",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,  # onedir
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 应用，不带控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

# macOS: 生成 .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_DISPLAY_NAME}.app",
        icon=icon_file,
        bundle_identifier="com.stellar.smart-terminal",
        info_plist={
            "CFBundleName": APP_DISPLAY_NAME,
            "CFBundleDisplayName": APP_DISPLAY_NAME,
            "CFBundleShortVersionString": "1.14.29",
            "CFBundleVersion": "1.14.29",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # 终端应用需要的权限说明（访问用户文件等由系统按需弹窗）。
            # NSAppleEventsUsageDescription 缺失时 Apple 事件会被静默拒绝
            # （不弹窗）——「在 Terminal.app 中运行」「移到废纸篓」依赖它
            "NSAppleEventsUsageDescription": "Stellar Smart Terminal needs to control other apps for automation features.",
            # 文件夹访问的弹窗说明文案（不配也会弹，配了显示应用自己的解释）
            "NSDocumentsFolderUsageDescription": "Terminal sessions and the file explorer need access to your project folders in Documents.",
            "NSDesktopFolderUsageDescription": "Terminal sessions and the file explorer need access to files on your Desktop.",
            "NSDownloadsFolderUsageDescription": "Terminal sessions and the file explorer need access to your Downloads folder.",
        },
    )
