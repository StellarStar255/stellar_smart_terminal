# Stellar Smart Terminal

English | [简体中文](README.zh-CN.md)

## Any screen size. A terminal that feels right.

A PyQt6-based smart terminal with file explorer, Git GUI, LLM proxy, and VS Code extension browser.

Latest release: [v1.35.41](docs/releases/v1.35.41.md) fixes edge scrolling and cross-page selection in Codex, including selections starting on pinned messages.

<img src="assets/smart_terminal.png" width="300" />

## Screenshot

![Stellar Smart Terminal screenshot](assets/smart_terminal_example1.png)

> Multi-pane layout: file explorer / Git, code editor, and multi-tab terminal in one window.

## Open a project in a new window

Paste an **existing folder path** into the **Directory** field and click **Open as New Window**. A separate project window opens and automatically runs your selected command preset. The path is remembered across restarts: select it from directory history and click the button to open it again. This also works with your configured Codex or Claude Code preset.

You can also click **⚡**, paste a long folder path directly into its search field, then press **Shift+Enter** to open a new window. **Enter** opens a new tab. The demo enlarges the path field and shows both windows so you can see that the original project stays open.

![Paste a long path into Quick Launch and press Shift+Enter to open a new window](assets/quick-launch-en.gif)

### Quick Launch in a tab

Choose a command preset, click **⚡** at the right end of the tab bar, paste an **existing folder path**, and press **Enter**. A new tab opens in that folder and automatically runs the selected preset—for example, your configured Codex or Claude Code command. The path is added to recent directories for next time. The other tabs keep their own working directories.

You can enter paths such as `/path/to/project`, `~/projects/my-app`, or `C:\projects\my-app` on Windows. Use **Shift+Enter** for a typed path or **Shift-click** a recent-directory entry to open a separate window. This opens an existing project folder; it does not create a folder or initialize a repository.

## Tutorial

An interactive tour pops up on first launch, walking you through Quick Launch (⚡), split panes, side panels, the command palette and more. Replay it anytime from Help › Tutorial.

![Onboarding tour](assets/onboarding-en.gif)

<details><summary>Chinese version</summary>

![Onboarding tour in Chinese](assets/onboarding-zh.gif)

</details>

### Terminal right-click menu

Plenty of handy tools live in the terminal's right-click menu: run presets in one click, per-project local quick commands, scrollback search, copy the current path, split panes, scroll speed and more.

![Terminal right-click menu](assets/context-menu-en.gif)

<details><summary>Chinese version</summary>

![Terminal right-click menu in Chinese](assets/context-menu-zh.gif)

</details>

## Features

- **Multi-tab Terminal** — session management
- **Project Quick Launch (⚡)** — paste a folder path to open a tab and run your preset; reuse recent directories
- **File Explorer & Editor**
- **Git GUI** — stage, commit, push, pull, diff, branch
- **LLM Proxy** — OpenAI-compatible
- **VS Code Extensions**
- **i18n** — English / Chinese

### Terminal recordings and export

Automatic recording is off. To capture output from now on, use **History → Start Recording Current Terminal** (also in File). Only that terminal is recorded; switching tabs does not change the target. Stop and save from History or File. Recording does not resume after restart and ends when the target exits, closes, or moves to another window. Older recordings remain available in History.

**Export** saves the current terminal’s retained screen and scrollback text, without starting a recording. It does not recover cleared or discarded text. For Codex and other dynamic interfaces, use the tool’s own session history for complete conversations; raw terminal recordings can contain repeated or misplaced text.

### SSH port-forwarding groups

In a host’s **Port Forwarding** dialog, check the rules you want and choose **Save Group As…**. Select a saved group, then use **Start Checked Ports** or **Stop Checked Ports** to operate on its members. Other ports are left alone; choosing a group does not change running ports. Drag rules to reorder them. Groups and order are saved separately for each host; green dots show running status, while checkboxes select group members.

Opening the dialog or choosing **Refresh** checks forwarding status against the current master connection and local listeners. Successful operations are recorded for that master so status can survive an app restart. Unverified rules show **Unknown status**; remote forwards rely on this record because SSH does not expose a portable list of remote listeners. **Restart** stops the selected rule before starting it, and stops if cancellation fails. Controls are disabled during operations, with progress and failure details shown on the rule. Recovery only cancels this app’s recorded rule on its current master; it never terminates another SSH process or rebuilds the master connection.

While the dialog is visible, status is checked in the background every 15 seconds without reconnecting or starting/stopping forwards. Background checks keep controls available, never overlap, and discard results made stale by your operations. Errors appear in the row’s status and tooltip without repeated popups. Invalid rules are reported individually so other rules can still be checked and started. If SSH explicitly confirms that a rule is already absent, stopping succeeds and restarting or deleting it can continue; other cancellation errors still stop the operation.

Select a rule and choose **Test Connection** for a manual diagnostic. It reports the forward state, listener connectivity, and destination TCP connectivity separately. Local forwarding checks the destination from the SSH host; remote forwarding checks it from this computer. SOCKS5 asks for an explicit `host:port` and tests the SOCKS5 CONNECT path. Local/remote listener and destination checks are independent, and TCP reachability does not verify application behavior or login. The check reuses the existing master without starting/stopping forwards or logging in again; if remote probing is unavailable, the destination is shown as unverified.

## Download

No Python required — grab a build from the [**Releases**](https://github.com/StellarStar255/stellar_smart_terminal/releases/latest) page.

1. Download `Stellar-Smart-Terminal-vX.Y.Z-macOS-arm64.dmg` (recommended; a zip is also available).
2. Open the DMG and drag `Stellar Smart Terminal.app` into the `Applications` folder.
3. Double-click to open. Since v1.16.4, macOS builds are Developer ID signed and notarized by Apple.

   <details><summary>Only for old versions (≤ v1.16.3)</summary>

   Older unsigned builds may be blocked on first launch — go to **System Settings → Privacy & Security** and click **Open Anyway**, or run:

   ```bash
   xattr -cr "/Applications/Stellar Smart Terminal.app"
   ```

   </details>

> Currently Apple Silicon (arm64) only.

## Quick Start (from source)

```bash
pip install -r requirements.txt
python app.py
```

## Requirements

- Python 3.10+
- PyQt6 >= 6.5.0
- pyte >= 0.8.0

## Troubleshooting

```shell
pip uninstall PyQt6 PyQt6-Qt6 PyQt6-sip -y
pip install PyQt6==6.7.1 PyQt6-Qt6==6.7.1 --no-cache-dir
```

## Guides

The following guides are written in Chinese.

- [Claude Code click-to-focus notifications (macOS)](docs/claude-code-notifications.md) — Configure a Stop hook so clicking a notification focuses the corresponding Smart Terminal window.
- [Packaging & Release](docs/PACKAGING.md) — The full workflow from packaging with `./build.sh` to publishing a GitHub Release.
- [macOS permissions & responsible process](docs/macos-permissions.md) — Troubleshoot microphone/camera crashes, configure Info.plist usage descriptions, and grant permissions that require manual approval.
- [Troubleshooting: PowerShell + proxy claude login](docs/troubleshooting-powershell-proxy-claude-login.md) — Set proxies on Windows with `$env:http_proxy = "..."` so the claude child process receives them.

## Build

Package as a standalone app (onedir mode, no Python required).

```bash
# macOS — creates dist/Stellar Smart Terminal.app and a drag-to-install .dmg
./build.sh
open "dist/Stellar Smart Terminal.app"
```

```bat
:: Windows — creates dist\StellarSmartTerminal\StellarSmartTerminal.exe
build.bat
```

> The script creates an isolated `.venv-build` venv, installs dependencies and PyInstaller, and (on macOS) generates the `.icns` icon from `assets/smart_terminal.png` and a drag-to-install DMG. See `smart_terminal.spec` for configuration.
>
> For the full release workflow (version bump, tag, GitHub Release), see [docs/PACKAGING.md](docs/PACKAGING.md).

## License

[MIT](LICENSE)
