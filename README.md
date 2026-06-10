# Stellar Smart Terminal

基于 PyQt6 的智能终端，集成文件管理、Git GUI、LLM 代理和 VS Code 扩展浏览。

A PyQt6-based smart terminal with file explorer, Git GUI, LLM proxy, and VS Code extension browser.

<img src="assets/smart_terminal.png" width="300" />

## Screenshot / 界面预览

![Stellar Smart Terminal 界面预览](assets/smart_terminal_example1.png)

> 多面板布局：文件管理 / Git、代码编辑器与多标签终端集成在一个窗口中。
> Multi-pane layout: file explorer / Git, code editor, and multi-tab terminal in one window.

## Features / 功能

- **Multi-tab Terminal / 多标签终端** — session management / 会话管理
- **File Explorer & Editor / 文件管理与编辑器**
- **Git GUI** — stage, commit, push, pull, diff, branch
- **LLM Proxy / LLM 代理** — OpenAI-compatible / 兼容 OpenAI 接口
- **VS Code Extensions / VS Code 扩展浏览**
- **i18n** — English / 中文

## Quick Start / 快速开始

```bash
pip install -r requirements.txt
python app.py
```

## Requirements / 依赖

- Python 3.10+
- PyQt6 >= 6.5.0
- pyte >= 0.8.0

## Troubleshooting / 故障排除
```shell
pip uninstall PyQt6 PyQt6-Qt6 PyQt6-sip -y
pip install PyQt6==6.7.1 PyQt6-Qt6==6.7.1 --no-cache-dir
```

## Guides / 指南

- [Claude Code 通知点击跳转 / Click-to-focus notifications (macOS)](docs/claude-code-notifications.md) — Stop hook 配置，点击通知直接跳回对应 Smart Terminal 窗口

## Build / 打包

打包为独立应用（onedir 模式，无需安装 Python）。
Package as a standalone app (onedir mode, no Python required).

```bash
# macOS — 产出 dist/Stellar Smart Terminal.app
./build.sh
open "dist/Stellar Smart Terminal.app"
```

```bat
:: Windows — 产出 dist\StellarSmartTerminal\StellarSmartTerminal.exe
build.bat
```

> 脚本会自动创建独立虚拟环境 `.venv-build`、安装依赖与 PyInstaller，macOS 下还会从 `assets/smart_terminal.png` 生成 `.icns` 图标。配置见 `smart_terminal.spec`。
> The script creates an isolated `.venv-build` venv, installs dependencies + PyInstaller, and (on macOS) generates the `.icns` icon from `assets/smart_terminal.png`. See `smart_terminal.spec` for configuration.

## License / 许可

[MIT](LICENSE)