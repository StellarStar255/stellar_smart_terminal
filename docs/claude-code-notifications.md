# Claude Code 通知点击跳转 / Click-to-focus notifications (macOS)

让 Claude Code 跑完任务时弹 macOS 通知，**点击通知直接跳回发起这次会话的那个 Smart Terminal 窗口**（多窗口/多 tab 也能精确定位）。

When Claude Code finishes a task, show a macOS notification — **clicking it jumps back to the exact Smart Terminal window that started the session** (works across multiple windows/tabs).

## How it works / 原理

Smart Terminal 在每个 PTY 子进程里注入两个环境变量：

- `SMART_TERMINAL=1` — 标记 "我跑在 Smart Terminal 里"
- `SMART_TERMINAL_PID=<pid>` — 启动这个 tab 的 Python GUI 进程 PID

Stop hook 脚本读到这两个变量后，用 `terminal-notifier -execute` 把一段 AppleScript bake 进通知里。点击时通过 PID 把对应 GUI 进程拉到前台。

> `TERM_PROGRAM` 已经被 Smart Terminal 固定设为 `vscode`（为了 TUI Unicode 渲染兼容），所以靠 bundle ID 没法区分窗口，只能用 PID。

## Setup / 配置步骤

1) 安装 `terminal-notifier`：
```shell
brew install terminal-notifier
```

2) 新建 `~/.claude/scripts/claude-stop-notify.sh`：
```bash
#!/bin/bash
# Claude Code Stop-hook notifier — click → focus the originating Smart Terminal.
set -u
DIR=$(basename "${PWD:-?}")
ARGS=(-title "Claude Code" -message "$DIR finished" -sound Glass)

if [ "${SMART_TERMINAL:-}" = "1" ] && [ -n "${SMART_TERMINAL_PID:-}" ]; then
  # Smart Terminal: 按 PID 精确激活那个 Python GUI 窗口
  ARGS+=(-execute "osascript -e 'tell application \"System Events\" to set frontmost of (first process whose unix id is ${SMART_TERMINAL_PID}) to true'")
else
  # 其他终端：按 bundle ID 激活整个 app
  case "${TERM_PROGRAM:-}" in
    iTerm.app)      BID="com.googlecode.iterm2" ;;
    Apple_Terminal) BID="com.apple.Terminal" ;;
    vscode)         BID="com.microsoft.VSCode" ;;
    Cursor)         BID="com.todesktop.230313mzl4w4u92" ;;
    WezTerm)        BID="com.github.wez.wezterm" ;;
    ghostty)        BID="com.mitchellh.ghostty" ;;
    WarpTerminal)   BID="dev.warp.Warp-Stable" ;;
    *)              BID="" ;;
  esac
  [ -n "$BID" ] && ARGS+=(-activate "$BID")
fi

exec /opt/homebrew/bin/terminal-notifier "${ARGS[@]}"
```

```shell
chmod +x ~/.claude/scripts/claude-stop-notify.sh
```

3) 在 `~/.claude/settings.json` 里挂上 Stop hook：
```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "$HOME/.claude/scripts/claude-stop-notify.sh" }
        ]
      }
    ]
  }
}
```

4) 首次点击通知时，系统会让 Smart Terminal（Python.app）申请 **辅助功能 / Accessibility** 权限——同意即可（AppleScript 的 `System Events` 需要）。

## Gotchas / 坑

- `SMART_TERMINAL_PID` 必须是 **Python GUI 进程的 PID**（`python app.py`），不是 PTY 子进程的 PID。Smart Terminal 在 `fork()` 之前抓取，否则 AppleScript 找不到对应 GUI 进程，点击会静默失败。
- 改完代码后，已经在跑的 Claude Code 会话用的还是旧 PID——**重启 Claude Code 会话**才会生效。
- 通知里的 PID 是生成时 bake 进去的。如果 Smart Terminal 中途重启了，旧通知点击会静默无效（无害）。
