"""Update only this repository's Apple notarization secrets via a hidden prompt.

Usage: python scripts/update_ci_notary.py --apple-id YOUR_APPLE_ID
Requires macOS, notarytool and an authenticated gh CLI. Credentials stay in memory.
"""
import argparse
import re
import subprocess
import sys

REPO = 'StellarStar255/stellar_smart_terminal'
TEAM_ID = '3QCL9WNFBB'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apple-id', required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Run this helper on macOS.')
    prompt = subprocess.run([
        'osascript', '-e', 'on run argv',
        '-e', 'text returned of (display dialog (item 1 of argv) default answer "" '
        'with hidden answer with title "Stellar Terminal — GitHub 公证配置" '
        'buttons {"取消", "验证并更新"} default button "验证并更新")',
        '-e', 'end run', '--',
        '输入 ' + args.apple_id + ' 的 Apple App 专用密码。\n'
        '脚本会先向 Apple 验证，再仅更新 Stellar Terminal 的两个公证 secret。\n'
        '密码不会写入文件、显示在日志或保存到仓库。',
    ], capture_output=True, text=True)
    if prompt.returncode:
        print('Cancelled; GitHub secrets were not changed.', flush=True)
        return 1
    password = prompt.stdout.rstrip('\r\n')
    del prompt
    if not password:
        print('Empty password; GitHub secrets were not changed.', flush=True)
        return 1
    print('Validating credentials with Apple…', flush=True)
    result = subprocess.run([
        'xcrun', 'notarytool', 'history', '--apple-id', args.apple_id,
        '--password', password, '--team-id', TEAM_ID, '--output-format', 'json',
    ], capture_output=True, text=True)
    if result.returncode:
        code = re.search(r'HTTP status code: (\d+)', result.stderr)
        print('Apple validation failed' + (' (HTTP ' + code[1] + ')' if code else '')
              + '; GitHub secrets were not changed.', flush=True)
        return 1
    del result
    for name, value in [('NOTARY_APPLE_ID', args.apple_id), ('NOTARY_PASSWORD', password)]:
        result = subprocess.run(['gh', 'secret', 'set', name, '--repo', REPO],
                                input=value, capture_output=True, text=True)
        if result.returncode:
            print('Failed to update ' + name + '; rerun to complete both updates.', flush=True)
            return 1
        print('Updated ' + name, flush=True)
    del password
    print('PASS: Apple verified; both notarization secrets updated. Signing secrets unchanged.',
          flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
