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
# 2. 生成 .icns 图标（仅 macOS 且缺失时）。
#    注意：必须用带透明留白的 mac 专用母版（图形占画布 ~80%、Apple 圆角），
#    满幅的 smart_terminal.png 会让 Dock 图标比其他 app 大一圈。
#    母版与 icns 由 scripts/make_mac_icon.py 生成并已提交，此处仅兜底。
# ---------------------------------------------------------------------------
ICNS="assets/smart_terminal.icns"
ICON_MAC_SRC="assets/smart_terminal_icon_mac.png"
if [ "$(uname)" = "Darwin" ] && [ ! -f "$ICNS" ]; then
    echo "==> Generating $ICNS from $ICON_MAC_SRC"
    ICONSET="$(mktemp -d)/smart_terminal.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 128 256 512; do
        sips -z "$sz" "$sz" "$ICON_MAC_SRC" \
            --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
        sips -z "$((sz * 2))" "$((sz * 2))" "$ICON_MAC_SRC" \
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
# 4. macOS: 打包 DMG（内含 .app + Applications 软链接，拖拽即安装）
# ---------------------------------------------------------------------------
DMG=""
if [ "$(uname)" = "Darwin" ]; then
    VERSION=$(grep -m1 'CFBundleShortVersionString' smart_terminal.spec | sed 's/[^0-9.]//g')
    DMG="dist/Stellar-Smart-Terminal-v${VERSION}-macOS-$(uname -m).dmg"
    echo "==> Creating $DMG"
    STAGING=$(mktemp -d)
    cp -R "dist/Stellar Smart Terminal.app" "$STAGING/"
    ln -s /Applications "$STAGING/Applications"
    hdiutil create -volname "Stellar Smart Terminal" -srcfolder "$STAGING" \
        -ov -format UDZO "$DMG" >/dev/null
    rm -rf "$STAGING"
fi

# ---------------------------------------------------------------------------
# 5. Linux: 把 onedir 打成 .deb（供 Ubuntu/Debian 用 apt/dpkg 安装）
#    布局：程序 → /opt/StellarSmartTerminal，/usr/bin 放启动包装，
#    外加 .desktop 桌面入口与 hicolor 图标，让应用出现在菜单/Dock。
# ---------------------------------------------------------------------------
DEB=""
if [ "$(uname)" = "Linux" ]; then
    VERSION=$(grep -m1 'CFBundleShortVersionString' smart_terminal.spec | sed 's/[^0-9.]//g')
    ARCH=$(dpkg --print-architecture 2>/dev/null || echo amd64)
    PKG="stellar-smart-terminal"
    DEB="dist/Stellar-Smart-Terminal-v${VERSION}-linux-${ARCH}.deb"
    echo "==> Creating $DEB"
    ROOT=$(mktemp -d)

    install -d "$ROOT/opt/StellarSmartTerminal"
    cp -R dist/StellarSmartTerminal/. "$ROOT/opt/StellarSmartTerminal/"

    install -d "$ROOT/usr/bin"
    cat > "$ROOT/usr/bin/$PKG" <<'EOF'
#!/bin/sh
exec /opt/StellarSmartTerminal/StellarSmartTerminal "$@"
EOF
    chmod 755 "$ROOT/usr/bin/$PKG"

    install -d "$ROOT/usr/share/icons/hicolor/512x512/apps"
    if command -v convert >/dev/null 2>&1; then
        convert assets/smart_terminal.png -resize 512x512 \
            "$ROOT/usr/share/icons/hicolor/512x512/apps/$PKG.png"
    else
        cp assets/smart_terminal.png \
            "$ROOT/usr/share/icons/hicolor/512x512/apps/$PKG.png"
    fi

    install -d "$ROOT/usr/share/applications"
    cat > "$ROOT/usr/share/applications/$PKG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Stellar Smart Terminal
Comment=Smart terminal with file explorer, Git GUI, and LLM proxy
Exec=$PKG
Icon=$PKG
Terminal=false
Categories=Development;Utility;
EOF

    install -d "$ROOT/DEBIAN"
    INSTALLED_KB=$(du -sk "$ROOT/opt" | cut -f1)
    cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Depends: libc6, libxcb-cursor0, libegl1, libxkbcommon0, libdbus-1-3
Installed-Size: $INSTALLED_KB
Maintainer: huangqiliang <goosehuangmatt@gmail.com>
Description: Stellar Smart Terminal
 A PyQt6-based smart terminal with file explorer, Git GUI, LLM proxy,
 and VS Code extension browser.
EOF

    dpkg-deb --root-owner-group --build "$ROOT" "$DEB"
    rm -rf "$ROOT"
fi

# ---------------------------------------------------------------------------
# 6. 输出位置提示
# ---------------------------------------------------------------------------
echo ""
echo "==> Build finished."
if [ "$(uname)" = "Darwin" ]; then
    echo "    App bundle : dist/Stellar Smart Terminal.app"
    echo "    DMG        : $DMG"
    echo "    Run        : open \"dist/Stellar Smart Terminal.app\""
fi
if [ "$(uname)" = "Linux" ]; then
    echo "    Deb        : $DEB"
    echo "    Install    : sudo apt install \"./$DEB\""
fi
echo "    Onedir     : dist/StellarSmartTerminal/"
