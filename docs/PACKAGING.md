# Packaging & Release / 打包与发布

如何把 Stellar Smart Terminal 打包成 macOS 应用(`.app` / `.dmg`)并发布到 GitHub Release。

## 自动发版(推荐)/ Automated release

仓库配置了 GitHub Actions(`.github/workflows/release.yml`):**推送 `v*` tag 即自动发版**——
macOS runner 跑 `build.sh` 产出 DMG + zip,Windows runner 跑 `build.bat` 产出 onedir zip,
两个平台都先跑离屏测试套件,产物自动挂到对应 Release(已存在的 release 只补传产物,
notes 不会被覆盖;新建的 release 用自动生成的 notes,之后可用 `gh release edit` 替换)。

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

Release notes 可参考 [v1.5.0](https://github.com/StellarStar255/stellar_smart_terminal/releases/tag/v1.5.0) 的结构:按模块(Terminal / Git panel / Remote / Editor)分组列变更,最后附下载安装说明。变更列表可从 `git log --oneline v1.5.0..HEAD` 整理。

补传/更新已有 release:

```bash
gh release upload v1.6.0 dist/xxx.dmg          # 追加产物
gh release edit v1.6.0 --notes-file notes.md   # 更新说明
```

## 已知限制 / Known limitations

- **未签名/未公证**:用户首次打开会被 Gatekeeper 拦截(macOS 15 Sequoia 起右键 → 打开已失效),需到 系统设置 → 隐私与安全性 → 仍要打开(Open Anyway),或执行 `xattr -cr "/Applications/Stellar Smart Terminal.app"`。彻底解决需要 Apple Developer 账号($99/年)做 codesign + notarytool 公证。
- **仅 arm64**:PyInstaller 跟随构建机架构,Intel 包需在 Intel Mac 上另行构建。
- 用户数据(配置、历史、书签)写在 bundle 外的用户目录,升级覆盖安装不会丢失。
