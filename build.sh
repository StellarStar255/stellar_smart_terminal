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
# 3. macOS 代码签名准备：自动探测 Developer ID 证书（可用 MACOS_CODESIGN_IDENTITY
#    覆盖；设为空串可强制跳过签名）。签名本身由 PyInstaller 在构建时完成
#    （见 smart_terminal.spec），这里只负责把身份传进去。
# ---------------------------------------------------------------------------
if [ "$(uname)" = "Darwin" ]; then
    if [ -z "${MACOS_CODESIGN_IDENTITY+x}" ]; then
        MACOS_CODESIGN_IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
            | sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' | head -1)
    fi
    export MACOS_CODESIGN_IDENTITY
    if [ -n "$MACOS_CODESIGN_IDENTITY" ]; then
        echo "==> Codesigning as: $MACOS_CODESIGN_IDENTITY"
    else
        echo "==> WARNING: no Developer ID certificate found — building UNSIGNED app"
    fi
fi

# ---------------------------------------------------------------------------
# 4. PyInstaller 构建（onedir + macOS .app bundle，见 smart_terminal.spec）
# ---------------------------------------------------------------------------
echo "==> Running PyInstaller"
"$VENV/bin/pyinstaller" --noconfirm smart_terminal.spec

# ---------------------------------------------------------------------------
# 5. macOS: 公证 + 盖章（stapling），然后打 DMG 并签名。
#    公证凭据来自钥匙串 profile（本地一次性配置 / CI 由 workflow 注入）：
#      xcrun notarytool store-credentials stellar-notary \
#        --apple-id <appleid> --team-id <team> --password <app专用密码>
#    没有证书或没有凭据时跳过对应步骤，仍产出可用（但未签名/未公证）的包。
# ---------------------------------------------------------------------------
DMG=""
if [ "$(uname)" = "Darwin" ]; then
    APP="dist/Stellar Smart Terminal.app"
    NOTARY_PROFILE="${NOTARY_PROFILE:-stellar-notary}"

    if [ -n "${MACOS_CODESIGN_IDENTITY:-}" ]; then
        echo "==> Verifying code signature"
        codesign --verify --deep --strict "$APP"

        # 凭据两种来源，取其一：
        # - CI：NOTARY_APPLE_ID / NOTARY_PASSWORD / NOTARY_TEAM_ID 环境变量
        # - 本地：一次性 `xcrun notarytool store-credentials stellar-notary ...`
        NOTARY_ARGS=()
        if [ -n "${NOTARY_APPLE_ID:-}" ] && [ -n "${NOTARY_PASSWORD:-}" ] \
                && [ -n "${NOTARY_TEAM_ID:-}" ]; then
            NOTARY_ARGS=(--apple-id "$NOTARY_APPLE_ID" --team-id "$NOTARY_TEAM_ID" \
                         --password "$NOTARY_PASSWORD")
        elif xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" \
                >/dev/null 2>&1; then
            NOTARY_ARGS=(--keychain-profile "$NOTARY_PROFILE")
        fi

        if [ ${#NOTARY_ARGS[@]} -gt 0 ]; then
            echo "==> Notarizing (usually 1-5 min)"
            NOTARY_ZIP=$(mktemp -d)/notarize.zip
            ditto -c -k --keepParent "$APP" "$NOTARY_ZIP"
            SUBMIT_OUT=$(xcrun notarytool submit "$NOTARY_ZIP" \
                "${NOTARY_ARGS[@]}" --wait 2>&1 | tee /dev/stderr)
            rm -rf "$(dirname "$NOTARY_ZIP")"
            if ! echo "$SUBMIT_OUT" | grep -q "status: Accepted"; then
                # notarytool 对 Invalid 结果也可能退出码 0，必须看状态文本。
                # 拿提交 id 查详细原因：xcrun notarytool log <id> <凭据参数>
                echo "ERROR: notarization not accepted — see output above" >&2
                exit 1
            fi
            # 盖章：把公证票据钉进 .app，用户离线首启也能通过 Gatekeeper
            xcrun stapler staple "$APP"
        else
            echo "==> WARNING: no notary credentials (env vars or profile '$NOTARY_PROFILE') — skipping notarization"
        fi
    fi

    VERSION=$(grep -m1 'CFBundleShortVersionString' smart_terminal.spec | sed 's/[^0-9.]//g')
    DMG="dist/Stellar-Smart-Terminal-v${VERSION}-macOS-$(uname -m).dmg"
    echo "==> Creating $DMG"
    STAGING=$(mktemp -d)
    cp -R "$APP" "$STAGING/"
    ln -s /Applications "$STAGING/Applications"
    hdiutil create -volname "Stellar Smart Terminal" -srcfolder "$STAGING" \
        -ov -format UDZO "$DMG" >/dev/null
    rm -rf "$STAGING"
    if [ -n "${MACOS_CODESIGN_IDENTITY:-}" ]; then
        codesign --sign "$MACOS_CODESIGN_IDENTITY" --timestamp "$DMG"
        # DMG 本身也要公证+盖章：用户打开 DMG 时 Gatekeeper 查的是 DMG 的
        # 公证状态，只公证里面的 .app 不够。内容已公证过，这次通常很快。
        if [ ${#NOTARY_ARGS[@]} -gt 0 ]; then
            echo "==> Notarizing DMG"
            SUBMIT_OUT=$(xcrun notarytool submit "$DMG" \
                "${NOTARY_ARGS[@]}" --wait 2>&1 | tee /dev/stderr)
            if ! echo "$SUBMIT_OUT" | grep -q "status: Accepted"; then
                echo "ERROR: DMG notarization not accepted — see output above" >&2
                exit 1
            fi
            xcrun stapler staple "$DMG"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 6. Linux: 把 onedir 打成 .deb（供 Ubuntu/Debian 用 apt/dpkg 安装）
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
StartupWMClass=$PKG
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
# 7. 输出位置提示
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
