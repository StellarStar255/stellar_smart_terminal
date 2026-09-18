#!/usr/bin/env bash
# 一次性把 macOS 签名 / 公证凭据写进 GitHub Actions secrets（Release 工作流用）。
#
# 用法（在自己的终端里跑，凭据只经 gh 直达 GitHub，不落盘、不进 git）：
#   scripts/setup_ci_signing_secrets.sh ~/Desktop/cert.p12
#
# cert.p12 的导出方法见 docs/PACKAGING.md「代码签名与公证」：钥匙串访问 →
# 我的证书 → 右键 "Developer ID Application: ..." → 导出（会连带私钥）。
# 跑完可以把 .p12 删掉；以后推 v* tag，CI 直接产出已签名+公证的 mac 包，
# 不再需要本地 build.sh + gh release upload --clobber 那一步。
set -euo pipefail

P12="${1:-}"
if [ -z "$P12" ] || [ ! -f "$P12" ]; then
  echo "用法: $0 <cert.p12>" >&2
  exit 1
fi
command -v gh >/dev/null || { echo "需要 gh CLI（brew install gh 并 gh auth login）" >&2; exit 1; }
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
echo "目标仓库: $REPO"

read -r -s -p "导出 .p12 时设置的密码: " CERT_PASS; echo
# 先在本地验证一下密码能解开 p12，免得把错的密码传上去
if ! openssl pkcs12 -in "$P12" -passin "pass:$CERT_PASS" -noout -legacy 2>/dev/null \
   && ! openssl pkcs12 -in "$P12" -passin "pass:$CERT_PASS" -noout 2>/dev/null; then
  echo "密码打不开这个 .p12，请重试" >&2
  exit 1
fi
read -r -p "Apple ID 邮箱: " APPLE_ID
read -r -s -p "该 Apple ID 的 app 专用密码（appleid.apple.com 生成）: " NOTARY_PASS; echo

base64 -i "$P12" | gh secret set MACOS_CERT_P12 --repo "$REPO"
printf '%s' "$CERT_PASS"   | gh secret set MACOS_CERT_PASSWORD --repo "$REPO"
printf '%s' "$APPLE_ID"    | gh secret set NOTARY_APPLE_ID --repo "$REPO"
printf '%s' "$NOTARY_PASS" | gh secret set NOTARY_PASSWORD --repo "$REPO"
unset CERT_PASS NOTARY_PASS

echo
echo "已写入 4 个 secrets："
gh secret list --repo "$REPO"
echo
echo "下一版推 tag 后，在 Release 工作流的 macOS job 里确认 'Import signing certificate'"
echo "没有打印 'building unsigned'，并对下载的 dmg 跑："
echo "  spctl -a -t open --context context:primary-signature -vv <dmg>   # 期望 Notarized Developer ID"
