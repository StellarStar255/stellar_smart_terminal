# Packaging & Release / 打包与发布

如何把 Stellar Smart Terminal 打包成 macOS 应用(`.app` / `.dmg`)并发布到 GitHub Release。

## 自动发版(推荐)/ Automated release

仓库配置了 GitHub Actions(`.github/workflows/release.yml`):**推送 `v*` tag 即自动发版**——
macOS runner 跑 `build.sh` 产出 DMG + zip,Windows runner 跑 `build.bat` 产出 onedir zip,
并用 **Inno Setup**(`installer.iss`)额外打出 `*-windows-x64-setup.exe` 安装包,
Linux runner(Ubuntu)跑 `build.sh` 把 onedir 打成 `*-linux-amd64.deb`(含 `.desktop`
桌面入口与图标),三个平台都跑离屏测试套件,产物自动挂到对应 Release(已存在的 release
只补传产物,notes 不会被覆盖;新建的 release 用自动生成的 notes,之后可用 `gh release edit` 替换)。

Ubuntu/Debian 用户:下载 `*-linux-amd64.deb`,`sudo apt install ./Stellar-Smart-Terminal-*.deb`
(自动拉取 `libxcb-cursor0` 等依赖),装好后在应用菜单搜 “Stellar Smart Terminal” 或命令行
跑 `stellar-smart-terminal`。

Windows 用户两种安装方式:

- **安装包(推荐)**:下载 `*-windows-x64-setup.exe`,双击安装。用户级安装(无需管理员、不弹
  UAC),自动建开始菜单 + 桌面快捷方式(向导里可取消勾选),自带卸载程序。
- **免安装 zip**:下载 `*-windows-x64.zip`,解压后运行 `StellarSmartTerminal.exe`。

> Inno Setup(`ISCC.exe`)已预装在 GitHub `windows-latest` runner 上,CI 无需额外安装。
> 本地编译安装包:`"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.8.0 installer.iss`
> (需先 `build.bat` 产出 `dist\StellarSmartTerminal\`)。

所以日常发版只需:

```bash
# 1. 更新版本号(见下文「更新版本号」),提交推送
# 2. 打 tag 并推送,剩下交给 CI(约 10 分钟)
git tag -a v1.7.0 -m "v1.7.0"
git push origin v1.7.0
gh run watch   # 可选:盯着跑完
```

下文的手动流程仍然有效,作为 CI 不可用时的备份,或需要本地验证产物时使用。

> 注:GitHub 托管的 macOS runner 均为 Apple Silicon,Intel mac 包需自备机器手动构建。

## 用 gh 盯发版 / Monitoring releases with gh

发版进度、产物、失败日志都可以用 [GitHub CLI](https://cli.github.com/)(`gh`)在命令行看,
不用开浏览器。

### 安装 gh(Windows)

```powershell
winget install --id GitHub.cli -e
```

> 装完**当前终端的 PATH 不会即时刷新**,新开一个终端 `gh` 才可用;或在当前终端执行
> `$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")`
> 立刻刷新。macOS 用 `brew install gh`。

首次使用先登录(交互式):`gh auth login` → 选 `GitHub.com` → `HTTPS` → 浏览器授权。

### 常用命令

```bash
gh run list --workflow=release.yml --limit 3   # 最近几次发版的状态
gh run watch <run-id> --exit-status            # 盯着某次跑完
gh run view <run-id>                           # 看各 job / 各步骤成败
gh run view <run-id> --log-failed              # 只看失败步骤的日志
gh release view v1.8.0 --json assets --jq '.assets[].name'  # 确认产物是否齐全
```

一次成功的发版,`gh release view` 应能看到四个产物:`*-windows-x64-setup.exe`、
`*-windows-x64.zip`、`*-macOS-arm64.dmg`、`*-macOS-arm64.zip`。

## CI 失败后重新发版 / Re-running a failed release

`v*` tag 的工作流跑的是 **tag 当时指向的 commit**,所以「在 main 上修一下」不会影响已推送的
tag——需要把 tag 移到修复后的 commit 再重新触发。两种情况:

- **偶发失败(flaky),代码无需改**:直接重跑失败的 job
  ```bash
  gh run rerun <run-id> --failed
  ```
  (比如共享 CI VM 太慢卡了性能计时测试 `test_terminal_reflow.py`——阈值已放宽到 2.0s,
  仍偶发的话重跑即可。)

- **需要改代码才能修**:在 main 上提交修复,再把 tag 强制移到新 commit:
  ```bash
  git commit -am "fix: ..." && git push origin main
  git tag -d v1.8.0 && git tag -a v1.8.0 -m "v1.8.0"
  git push origin v1.8.0 --force
  ```
  重新触发后所有产物会重建并覆盖上传(`gh release upload --clobber`),已发布几分钟的
  release 补成完整版。

## 前置要求 / Prerequisites

- macOS(Apple Silicon 机器上打出的是 arm64 包,Intel Mac 无法运行)
- `python3`(3.10+)
- 发布需要 [GitHub CLI](https://cli.github.com/)(`gh`),且已 `gh auth login`

打包脚本用到的 `sips`、`iconutil`、`hdiutil` 都是 macOS 自带工具,无需安装。

## 一键打包 / Build

```bash
./build.sh
```

脚本会自动完成:

1. 创建/复用独立构建虚拟环境 `.venv-build/`(不污染系统环境)
2. 安装 `requirements.txt` 依赖 + PyInstaller
3. 从 `assets/smart_terminal.png` 生成 `.icns` 图标(已存在则跳过)
4. 用 PyInstaller(配置见 `smart_terminal.spec`)构建 onedir 和 `.app` bundle
5. 生成拖拽安装的 DMG(内含 `.app` + `Applications` 软链接)

产物在 `dist/` 下:

| 产物 | 用途 |
|------|------|
| `Stellar Smart Terminal.app` | 应用本体,`open` 可直接运行验证 |
| `Stellar-Smart-Terminal-v<版本>-macOS-arm64.dmg` | 分发给用户(推荐) |
| `StellarSmartTerminal/` | onedir 原始输出,一般不直接分发 |

打包后建议本地跑一次验证:

```bash
open "dist/Stellar Smart Terminal.app"
```

如需从头重新打包,删掉 `build/`、`dist/`(必要时连同 `.venv-build/`)再跑脚本。

## 发布新版本 / Release

以发布 `v1.6.0` 为例:

### 1. 更新版本号

三处需要一致:

- `smart_terminal.spec` — `CFBundleShortVersionString` 和 `CFBundleVersion`
- `pyproject.toml` — `version`

DMG 文件名里的版本号由 `build.sh` 自动从 spec 读取,不用手改。

```bash
git add smart_terminal.spec pyproject.toml
git commit -m "chore: bump version to 1.6.0"
```

### 2. 打包并生成 zip

```bash
./build.sh
# zip 作为 DMG 之外的备选下载(ditto 保留 .app bundle 结构,不能用普通 zip 命令)
ditto -c -k --keepParent "dist/Stellar Smart Terminal.app" \
    "dist/Stellar-Smart-Terminal-v1.6.0-macOS-arm64.zip"
```

可挂载 DMG 检查内容:

```bash
hdiutil attach dist/Stellar-Smart-Terminal-v1.6.0-macOS-arm64.dmg -nobrowse -readonly
ls "/Volumes/Stellar Smart Terminal/"   # 应有 .app 和 Applications 软链接
hdiutil detach "/Volumes/Stellar Smart Terminal"
```

### 3. 打 tag 并创建 Release

```bash
git push origin main
git tag -a v1.6.0 -m "v1.6.0"
git push origin v1.6.0

gh release create v1.6.0 \
    dist/Stellar-Smart-Terminal-v1.6.0-macOS-arm64.dmg \
    dist/Stellar-Smart-Terminal-v1.6.0-macOS-arm64.zip \
    --title "v1.6.0" --notes-file /tmp/release_notes.md
```

Release notes 可参考 [v1.5.0](https://github.com/StellarStar255/stellar_smart_terminal/releases/tag/v1.5.0) 的结构:按模块(Terminal / Git panel / Remote / Editor)分组列变更,最后附下载安装说明。

起草可用脚本自动分组(产出的是草稿,发布前必须手工润色成面向用户的说明):

```bash
python3 scripts/draft_release_notes.py --version v1.14.0 -o /tmp/release_notes.md
# 默认取「最近一个 tag..HEAD」;跨版本用 --since v1.12.0 指定起点
```

补传/更新已有 release:

```bash
gh release upload v1.6.0 dist/xxx.dmg          # 追加产物
gh release edit v1.6.0 --notes-file notes.md   # 更新说明
```

## 应用内更新 / In-app updates

设置 ⚙ 菜单 →「检查更新…」(app_updater.py):查 GitHub Releases 最新 tag,
打包版可一键「下载并安装」——
- macOS:下载 `*-macOS-arm64.zip`、ditto 解压、等应用退出后分阶段换包
  (失败自动回滚)并重启;
- Windows:下载 `*-windows-x64-setup.exe`,等应用退出后 `/SILENT /NORESTART`
  静默原地升级(installer.iss 固定 AppId + PrivilegesRequired=lowest,无 UAC)
  并重启;
- Linux:下载 `*-linux-amd64.deb`,等应用退出后 `pkexec apt-get install`
  原地升级(弹一次系统授权密码框——deb 装进系统目录需要 root,这是发行版
  安全模型;无 polkit 的环境自动放弃、旧版保持完好)并重启;
- 源码运行退化为打开发布页。
运行时版本:mac 读自身 Info.plist,Windows/Linux 冻结版读随包的
pyproject.toml(spec datas 已包含),源码运行读仓库 pyproject——发版无需
额外维护版本号。**依赖每个 release 都带对应平台产物**(CI 已自动上传)。
注:应用自身进程下载的文件不带 quarantine 标记,未签名状态下换包重启不会再
触发 Gatekeeper;换包脚本仍带 `xattr -dr` 兜底。

## 已知限制 / Known limitations

- **未签名/未公证**:用户首次打开会被 Gatekeeper 拦截(macOS 15 Sequoia 起右键 → 打开已失效),需到 系统设置 → 隐私与安全性 → 仍要打开(Open Anyway),或执行 `xattr -cr "/Applications/Stellar Smart Terminal.app"`。彻底解决需要 Apple Developer 账号($99/年)做 codesign + notarytool 公证。
- **仅 arm64**:PyInstaller 跟随构建机架构,Intel 包需在 Intel Mac 上另行构建。
- 用户数据(配置、历史、书签)写在 bundle 外的用户目录,升级覆盖安装不会丢失。
- **子程序权限 / 责任进程**:在终端里启动的程序用麦克风/摄像头/语音识别等受保护资源时,权限归属到发起进程(本 `.app`),须由 `smart_terminal.spec` 的 `info_plist` 声明 `NS*UsageDescription`(已声明 29 项),否则子进程调用即闪退。屏幕录制/辅助功能/完全磁盘访问/输入监控四类无 plist 键,需用户手动授权。完整说明见 [docs/macos-permissions.md](macos-permissions.md)。
