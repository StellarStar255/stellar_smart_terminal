#!/usr/bin/env bash
# 一次性把 macOS 签名 / 公证凭据写进 GitHub Actions secrets（Release 工作流用）。
#
# 零手输版：不需要先导 .p12、不需要在终端里敲密码。脚本自己：
#   1. 从登录钥匙串导出签名身份（系统会弹「允许」对话框，点允许即可）；
#      .p12 的包装密码由脚本随机生成，只用来在 GitHub secrets 里保护这份文件。
#   2. 弹原生对话框问 Apple ID 邮箱和 app 专用密码（密码隐藏输入）。
#   3. 上传前本地验证：p12 里确有 Developer ID 证书；公证凭据能登上 notarytool。
#   4. 经 gh 直传 4 个 secrets。凭据不落盘、不进 git、不打印到任何日志。
#
# 用法（可由自动化直接启动；所有需要人参与的地方都是系统弹窗）：
#   scripts/setup_ci_signing_secrets.sh
#
# 跑完之后推 v* tag，CI 直接产出已签名+公证的 mac 包，不再需要本地
# build.sh + gh release upload --clobber 那一步。
set -euo pipefail

TITLE="Stellar CI 签名配置"
TEAM_ID="3QCL9WNFBB"
LOGIN_KC="$HOME/Library/Keychains/login.keychain-db"

command -v gh >/dev/null || { echo "需要 gh CLI（brew install gh 并 gh auth login）" >&2; exit 1; }
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "目标仓库: $REPO"

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
P12="$TMP/cert.p12"

# ---- 原生对话框工具（值只回到脚本变量，不 echo） ----------------------------
notify() {
  osascript -e 'on run argv' \
    -e 'display notification (item 1 of argv) with title (item 2 of argv)' \
    -e 'end run' -- "$1" "$TITLE" >/dev/null 2>&1 || true
}
ask_text() {  # $1 提示  $2 默认值
  osascript -e 'on run argv' \
    -e 'text returned of (display dialog (item 1 of argv) default answer (item 2 of argv) with title (item 3 of argv) buttons {"取消", "确定"} default button "确定")' \
    -e 'end run' -- "$1" "$2" "$TITLE"
}
ask_secret() {  # $1 提示
  osascript -e 'on run argv' \
    -e 'text returned of (display dialog (item 1 of argv) default answer "" with title (item 2 of argv) with hidden answer buttons {"取消", "确定"} default button "确定")' \
    -e 'end run' -- "$1" "$TITLE"
}
fail_dialog() {
  osascript -e 'on run argv' \
    -e 'display dialog (item 1 of argv) with title (item 2 of argv) buttons {"好"} default button "好" with icon stop' \
    -e 'end run' -- "$1" "$TITLE" >/dev/null 2>&1 || true
  echo "$1" >&2
  exit 1
}

# ---- 1. 导出签名身份 --------------------------------------------------------
if ! security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application"; then
  fail_dialog "登录钥匙串里没有 Developer ID Application 证书，无法配置 CI 签名。"
fi
notify "接下来系统会请求导出签名私钥，请点「允许」。"
CERT_PASS="$(openssl rand -base64 30 | tr -d '/+=' | cut -c1-32)"
if ! security export -k "$LOGIN_KC" -t identities -f pkcs12 -P "$CERT_PASS" -o "$P12" 2>"$TMP/export.err"; then
  fail_dialog "导出签名身份失败：$(head -c 300 "$TMP/export.err")"
fi
# 密码能解开，且里面确有 Developer ID 证书（build.sh 按这个名字挑身份）
if ! { openssl pkcs12 -in "$P12" -passin "pass:$CERT_PASS" -nokeys -legacy 2>/dev/null \
       || openssl pkcs12 -in "$P12" -passin "pass:$CERT_PASS" -nokeys 2>/dev/null; } \
     | grep -q "Developer ID Application"; then
  fail_dialog "导出的 .p12 里没有 Developer ID Application 证书。"
fi
echo "签名身份已导出并验证。"

# ---- 2. 公证凭据 ------------------------------------------------------------
DEFAULT_ID="$(git config user.email 2>/dev/null || true)"
APPLE_ID="$(ask_text "公证用的 Apple ID 邮箱：" "$DEFAULT_ID")" || fail_dialog "已取消。"
[ -n "$APPLE_ID" ] || fail_dialog "Apple ID 不能为空。"
NOTARY_PASS="$(ask_secret "该 Apple ID 的 app 专用密码（appleid.apple.com → 登录与安全 → App 专用密码）：")" || fail_dialog "已取消。"
[ -n "$NOTARY_PASS" ] || fail_dialog "app 专用密码不能为空。"

echo "正在向 Apple 验证公证凭据……"
# notarytool 会吃 shell 里的代理变量，走本地代理常超时，直连验证
if ! env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
     xcrun notarytool history --apple-id "$APPLE_ID" --password "$NOTARY_PASS" \
     --team-id "$TEAM_ID" >/dev/null 2>"$TMP/notary.err"; then
  fail_dialog "公证凭据验证失败（Apple ID 或 app 专用密码不对，或网络不通）：$(grep -v -i password "$TMP/notary.err" | head -c 300)"
fi
echo "公证凭据验证通过。"

# ---- 3. 写入 secrets --------------------------------------------------------
base64 -i "$P12"           | gh secret set MACOS_CERT_P12      --repo "$REPO"
printf '%s' "$CERT_PASS"   | gh secret set MACOS_CERT_PASSWORD --repo "$REPO"
printf '%s' "$APPLE_ID"    | gh secret set NOTARY_APPLE_ID     --repo "$REPO"
printf '%s' "$NOTARY_PASS" | gh secret set NOTARY_PASSWORD     --repo "$REPO"
unset CERT_PASS NOTARY_PASS

echo
echo "已写入 4 个 secrets："
gh secret list --repo "$REPO"
notify "CI 签名 secrets 已配置完成。"
echo
echo "下一版推 tag 后，在 Release 工作流的 macOS job 里确认 'Import signing certificate'"
echo "没有打印 'building unsigned'，并对下载的 dmg 跑："
echo "  spctl -a -t open --context context:primary-signature -vv <dmg>   # 期望 Notarized Developer ID"
