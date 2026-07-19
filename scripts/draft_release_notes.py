#!/usr/bin/env python3
"""从上个 tag 以来的 commit 起草 release notes 草稿。

    python3 scripts/draft_release_notes.py            # 上个 tag..HEAD → stdout
    python3 scripts/draft_release_notes.py --since v1.12.0 -o /tmp/notes.md

产出的是**草稿**：按既往 release 的分区风格（🚀 性能 / ✍️ 编辑器 / …）把
commit 分好组、原文列出，供手工润色成面向用户的双语说明——不是直接可发的
最终稿（发版说明必须手写，见项目惯例）。

分组规则：conventional commit 的 type/scope 优先，其次匹配主题关键词；
chore/docs/refactor/test 归入草稿末尾的「默认不进说明」区，润色时自行取舍。
"""
import argparse
import re
import subprocess
import sys

# (分区标题, scope 集合, 主题关键词)——从上到下先匹配先赢。
# 标题为「中文 / English」双语（2026-07 起 release notes 必须中英双语）。
SECTIONS = [
    ("🚀 性能 / Performance", {"perf"},
     ("perf", "性能", "lag", "卡顿", "speed")),
    ("✍️ 编辑器 / Editor", {"editor", "md-preview", "file-editor", "file_editor"},
     ("editor", "markdown", "md preview", "preview", "编辑器")),
    ("🖥️ 终端 / Terminal", {"terminal", "term", "tmux", "pty"},
     ("terminal", "tmux", "pty", "终端", "scrollback")),
    ("🌿 Git", {"git", "git-widget", "git_widget"},
     ("git ", "commit", "branch", "checkout")),
    ("📁 文件管理 / Files", {"explorer", "remote", "sftp"},
     ("explorer", "sftp", "remote file", "文件管理")),
    ("🎛️ 界面与默认值 / UI & Defaults", {"ui", "theme", "toolbar", "i18n", "macos", "window"},
     ("theme", "toolbar", "window", "ui", "主题", "工具栏", "窗口", "opacity")),
    ("📦 打包与 CI / Packaging & CI", {"ci", "build", "release", "packaging"},
     ("ci", "workflow", "package", "installer", "打包")),
]
FALLBACK_FIX = "🔧 修复 / Bug Fixes"
SKIP_TYPES = {"chore", "docs", "refactor", "test", "style"}
CONV_RE = re.compile(r"^(?P<type>\w+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<subj>.+)$")


def _git(*args) -> str:
    return subprocess.run(["git", *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def _kw_hit(low: str, kw: str) -> bool:
    """ASCII 关键词按词边界匹配（防止 pty ⊂ emPTY 这类子串误命中），
    中文关键词无词边界概念，按包含匹配。"""
    if kw.isascii():
        return re.search(rf'\b{re.escape(kw)}\b', low) is not None
    return kw in low


def classify(ctype: str, scope: str, subject: str) -> str:
    low = subject.lower()
    for title, scopes, keywords in SECTIONS:
        if scope in scopes:
            return title
        if ctype == "perf" and title.startswith("🚀"):
            return title
        if ctype in ("ci", "build") and title.startswith("📦"):
            return title
        if any(_kw_hit(low, kw) for kw in keywords):
            return title
    return FALLBACK_FIX if ctype == "fix" else "🎛️ 界面与默认值"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", help="起点 tag（默认取最近一个 tag）")
    ap.add_argument("--version", help="标题版本号（默认按起点 tag 猜下一个 minor）")
    ap.add_argument("-o", "--output", help="写入文件（默认打印到 stdout）")
    args = ap.parse_args()

    since = args.since or _git("describe", "--tags", "--abbrev=0")
    log = _git("log", f"{since}..HEAD", "--no-merges",
               "--pretty=%h\t%s")

    grouped: dict[str, list[str]] = {}
    skipped: list[str] = []
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, subject = line.split("\t", 1)
        m = CONV_RE.match(subject)
        ctype = (m.group("type") if m else "").lower()
        scope = ((m.group("scope") or "") if m else "").lower()
        subj = m.group("subj") if m else subject
        # 版本号 bump、发版工件类提交不进说明
        if re.search(r"bump version|release v?\d", subject, re.I):
            continue
        entry = (f"- **[待润色]** {subj}（`{sha}`）\n"
                 f"- **[English/待翻译]** …")
        if ctype in SKIP_TYPES:
            skipped.append(entry)
            continue
        grouped.setdefault(classify(ctype, scope, subj), []).append(entry)

    version = args.version or "vNEXT"
    out = [f"<!-- {version} release notes 草稿：由 scripts/draft_release_notes.py "
           f"从 {since}..HEAD 生成，需手工润色成面向用户的中英双语说明后再发布：",
           "     每条中文条目后紧跟对应英文条目，导语和下载区也要中英各一段 -->",
           "",
           "（一句话导语：本次的主角是 ……。无破坏性变更时注明配置向后兼容。）",
           "",
           "(English intro: one sentence summarizing this release. Mention config "
           "backward-compatibility if there are no breaking changes.)",
           ""]
    # 按 SECTIONS 定义的顺序输出，修复区殿后
    order = [t for t, _, _ in SECTIONS] + [FALLBACK_FIX]
    for title in order:
        if title in grouped:
            out += [f"## {title}", *grouped[title], ""]
    out += ["---",
            "",
            "**下载**：Windows 用 `*-setup.exe`；macOS 推荐 `*.dmg`（arm64）"
            "或应用内升级；Linux 用 `*.deb`。",
            "",
            "**Downloads**: Windows — `*-setup.exe`; macOS — `*.dmg` (arm64) "
            "recommended, or upgrade in-app; Linux — `*.deb`.",
            ""]
    if skipped:
        out += ["<!-- 以下为 chore/docs/refactor/test，默认不进说明，按需取用",
                *skipped, "-->", ""]

    text = "\n".join(out)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"draft written to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
