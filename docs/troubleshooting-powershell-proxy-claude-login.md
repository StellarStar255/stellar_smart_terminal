# 排障记录：PowerShell + 代理下 claude login 失败 / 代理不生效

- 日期：2026-07-23
- 环境：Windows 11，默认 shell 为 PowerShell（`powershell` / `pwsh`），
  本机代理（如 Clash Verge Rev，混合端口 7897）
- 症状：在 Smart Terminal 里用「Claude ...（with proxy）」预设启动后，
  `claude` 登录（login）失败 / 连不上，像是完全没走代理；
  但同一个代理在浏览器、其它终端里都正常。

## 结论（TL;DR）

预设里用 `set http_proxy=...` 设代理，这是 **cmd.exe 的语法**。
在 PowerShell 里 `set` 不会设置进程环境变量（PowerShell 的 `set` 是
`Set-Variable` 的别名，只设 PowerShell 变量，不进环境块），于是被拉起的
子进程 `claude` **拿不到 `http_proxy` / `https_proxy`**，裸连
Anthropic → login 失败。

正确做法：PowerShell 下必须用 `$env:` 形式设置环境变量，claude 才能继承：

```powershell
$env:http_proxy = "http://127.0.0.1:7897"
$env:https_proxy = "http://127.0.0.1:7897"
claude --model fable
```

各 shell 对照：

| shell | 设置代理的写法 |
| --- | --- |
| PowerShell / pwsh（Windows 默认） | `$env:http_proxy = "http://127.0.0.1:7897"` |
| cmd.exe | `set http_proxy=http://127.0.0.1:7897` |
| bash / zsh（macOS/Linux） | `export http_proxy=http://127.0.0.1:7897` |

## 诊断判据

- 在启动 `claude` 前，先在同一 PowerShell 里执行 `echo $env:http_proxy`：
  若为空，说明前面的 `set http_proxy=...` 根本没生效（PowerShell 语法不对）。
- 用 `$env:` 形式设置后再 `echo $env:http_proxy`，能打印出代理地址，
  再启动 `claude` login 即恢复正常。

## 修复

### 应用层（默认预设按 shell 生成正确语法）

`main_window_config.py` 生成内置预设时，检测默认 shell：
若是 PowerShell / pwsh，代理命令用 `$env:http_proxy = "..."`；
cmd.exe 仍用 `set`，Unix 仍用 `export`。

同时在「管理预设」对话框里加了常驻提示与 placeholder 示例，
说明代理 + PowerShell 必须用 `$env:` 形式。

### 已有配置的迁移提醒

内置预设只在**首次、无任何预设**时生成。若你此前已保存过带
`set http_proxy=...` 的「Claude ...（with proxy）」预设，它**不会自动更新**——
需要手动打开「管理预设」，把那两行改成上面的 `$env:` 形式（对话框内有提示）。
