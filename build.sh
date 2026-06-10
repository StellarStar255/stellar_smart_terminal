#!/usr/bin/env bash
# Stellar Smart Terminal — macOS 打包脚本
# 用法: ./build.sh
# 产物: dist/Stellar Smart Terminal.app（macOS）/ dist/StellarSmartTerminal/（onedir）
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv-build"
PYTHON="${PYTHON:-python3}"

# ---------------------------------------------------------------------------
# 1. 创建/复用独立构建虚拟环境（不污染系统环境）
# ---------------------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
    echo "==> Creating build venv: $VENV"
    "$PYTHON" -m venv "$VENV"
fi

echo "==> Installing dependencies + PyInstaller"
"$VENV/bin/python" -m pip install --upgrade pip --quiet
"$VENV/bin/python" -m pip install -r requirements.txt pyinstaller --quiet

# ---------------------------------------------------------------------------
# 2. 生成 .icns 图标（仅 macOS 且缺失时；从 2048x2048 png 派生）
# ---------------------------------------------------------------------------
ICNS="assets/smart_terminal.icns"
if [ "$(uname)" = "Darwin" ] && [ ! -f "$ICNS" ]; then
    echo "==> Generating $ICNS from assets/smart_terminal.png"
    ICONSET="$(mktemp -d)/smart_terminal.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 128 256 512; do
        sips -z "$sz" "$sz" assets/smart_terminal.png \
            --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
        sips -z "$((sz * 2))" "$((sz * 2))" assets/smart_terminal.png \
            --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$ICNS"
    rm -rf "$(dirname "$ICONSET")"
fi

# ---------------------------------------------------------------------------
# 3. PyInstaller 构建（onedir + macOS .app bundle，见 smart_terminal.spec）
# ---------------------------------------------------------------------------
echo "==> Running PyInstaller"
"$VENV/bin/pyinstaller" --noconfirm smart_terminal.spec

# ---------------------------------------------------------------------------
# 4. 输出位置提示
# ---------------------------------------------------------------------------
echo ""
echo "==> Build finished."
if [ "$(uname)" = "Darwin" ]; then
    echo "    App bundle : dist/Stellar Smart Terminal.app"
    echo "    Run        : open \"dist/Stellar Smart Terminal.app\""
fi
echo "    Onedir     : dist/StellarSmartTerminal/"
