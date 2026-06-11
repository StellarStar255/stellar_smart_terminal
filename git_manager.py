"""
Git 管理器后端
提供 Git 仓库操作的核心功能
"""
import os
import re
import signal
import subprocess
import sys
import threading
from enum import Enum
from dataclasses import dataclass
from typing import List, Tuple, Optional
from PyQt6.QtCore import QObject, pyqtSignal, QFileSystemWatcher, QTimer

from i18n import t


# 网络类 git 操作（push / pull / fetch 等）的统一超时秒数。
# 本地操作仍用 _run_git 的默认 30s。
GIT_NETWORK_TIMEOUT = 120

# Windows: 窗口化（无控制台）应用 spawn 子进程时若不加 CREATE_NO_WINDOW,
# 每次都会闪出一个 conhost 控制台窗口；状态刷新每 5 秒跑多个 git 命令,
# 不加这个标志在 Windows 上表现为持续闪窗 + 卡顿。
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class FileStatus(Enum):
    """文件状态枚举"""
    MODIFIED = 'M'      # 已修改
    ADDED = 'A'         # 新增
    DELETED = 'D'       # 已删除
    RENAMED = 'R'       # 重命名
    COPIED = 'C'        # 复制
    UNTRACKED = '?'     # 未跟踪
    IGNORED = '!'       # 忽略
    UNMERGED = 'U'      # 未合并


@dataclass
class GitFile:
    """Git 文件状态数据类"""
    path: str                           # 文件路径（相对于仓库根目录）
    status: FileStatus                  # 文件状态
    old_path: Optional[str] = None      # 重命名时的旧路径
    is_conflict: bool = False           # 是否为未解决的合并冲突（UU/AA/DD 等）


@dataclass
class GitStash:
    """Git stash 条目数据类"""
    index: int          # 序号（stash@{N} 中的 N）
    ref: str            # 完整引用，如 'stash@{0}'
    branch: str         # 创建 stash 时所在的分支（解析不出时为空串）
    message: str        # stash 描述
    date: str           # 相对时间，如 '2 hours ago'


@dataclass
class GitCommit:
    """Git 提交记录数据类"""
    hash: str           # 提交哈希
    short_hash: str     # 短哈希
    author: str         # 作者
    date: str           # 日期
    message: str        # 提交信息


@dataclass
class GitBranch:
    """Git 分支数据类"""
    name: str           # 分支名
    is_current: bool    # 是否为当前分支
    is_remote: bool     # 是否为远程分支


class GitManager(QObject):
    """Git 仓库管理器"""

    # 信号
    status_changed = pyqtSignal()       # 状态变更信号
    error_occurred = pyqtSignal(str)    # 错误发生信号
    op_output = pyqtSignal(str, str)    # 操作输出信号 (kind, 合并后的 stdout+stderr)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._repo_path: Optional[str] = None
        self._watcher: Optional[QFileSystemWatcher] = None

        # 备份刷新定时器（5秒间隔）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(5000)
        self._refresh_timer.timeout.connect(self._on_timer_refresh)

        # 防抖定时器
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(300)
        self._debounce_timer.timeout.connect(self._emit_status_changed)

        # 跟踪在跑的 git 子进程，便于关闭程序或用户取消时一次性 kill
        # （subprocess.run 是阻塞调用，worker 线程在 push/pull hang 住时只能
        # 通过 kill 子进程来让 communicate() 立刻返回。）
        self._active_procs: set = set()
        self._proc_lock = threading.Lock()

        # 应用级 git 代理：通过 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY 注入子进程
        # 环境，覆盖 push/pull/fetch/clone/ls-remote 等所有走 HTTPS 的 git 操作。
        # 注意：对 SSH 协议的远端（git@host:repo.git）不生效；那种需要 ~/.ssh/config 配置。
        self._proxy: str = ''

    def set_proxy(self, url: str):
        """设置应用内 git 代理（仅影响本程序里的 git 子进程，不改全局 git config）。

        Args:
            url: 形如 'http://127.0.0.1:7897'，传空字符串表示不使用代理。
        """
        self._proxy = (url or '').strip()

    def get_proxy(self) -> str:
        return self._proxy

    def set_repository(self, path: str) -> bool:
        """设置仓库路径

        Args:
            path: 目录路径

        Returns:
            是否成功设置（目录是否为 Git 仓库）
        """
        # 检查是否为 Git 仓库
        git_dir = self._find_git_dir(path)
        if not git_dir:
            self._repo_path = None
            self._stop_watching()
            return False

        # 获取仓库根目录 (prefer git rev-parse for worktree correctness)
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--show-toplevel'],
                cwd=path, capture_output=True, text=True, timeout=5,
                encoding='utf-8', errors='replace',
                creationflags=SUBPROCESS_FLAGS,
            )
            if result.returncode == 0:
                repo_root = result.stdout.strip()
            else:
                repo_root = os.path.dirname(git_dir)
        except Exception:
            repo_root = os.path.dirname(git_dir)

        # 如果是同一个仓库，不需要重新设置
        if self._repo_path == repo_root:
            return True

        self._repo_path = repo_root
        self._setup_watcher(git_dir)
        self._refresh_timer.start()
        return True

    def _find_git_dir(self, path: str) -> Optional[str]:
        """查找 .git 目录（支持普通仓库和 git worktree）

        Args:
            path: 起始目录

        Returns:
            .git 目录路径，如果不是 Git 仓库则返回 None
        """
        current = os.path.abspath(path)
        while current != os.path.dirname(current):  # 不是根目录
            git_path = os.path.join(current, '.git')
            if os.path.isdir(git_path):
                return git_path
            # Support git worktrees where .git is a file containing "gitdir: <path>"
            if os.path.isfile(git_path):
                try:
                    with open(git_path, 'r') as f:
                        content = f.read().strip()
                    if content.startswith('gitdir:'):
                        gitdir = content[len('gitdir:'):].strip()
                        if not os.path.isabs(gitdir):
                            gitdir = os.path.join(current, gitdir)
                        gitdir = os.path.normpath(gitdir)
                        if os.path.isdir(gitdir):
                            return gitdir
                except Exception:
                    pass
            current = os.path.dirname(current)
        return None

    def _setup_watcher(self, git_dir: str):
        """设置文件监视器

        Args:
            git_dir: .git 目录路径
        """
        self._stop_watching()

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

        # 监视关键文件
        index_path = os.path.join(git_dir, 'index')
        head_path = os.path.join(git_dir, 'HEAD')
        refs_path = os.path.join(git_dir, 'refs')

        for path in [index_path, head_path]:
            if os.path.exists(path):
                self._watcher.addPath(path)

        if os.path.isdir(refs_path):
            self._watcher.addPath(refs_path)

    def _stop_watching(self):
        """停止文件监视"""
        if self._watcher:
            self._watcher.deleteLater()
            self._watcher = None
        self._refresh_timer.stop()

    def _on_file_changed(self, path: str):
        """文件变更回调"""
        # Re-add path if QFileSystemWatcher dropped it (common when files are
        # replaced atomically, e.g. git's index and HEAD files)
        if self._watcher and os.path.exists(path):
            watched = self._watcher.files()
            if path not in watched:
                self._watcher.addPath(path)
        self._debounce_timer.start()

    def _on_dir_changed(self, path: str):
        """目录变更回调"""
        self._debounce_timer.start()

    def _on_timer_refresh(self):
        """定时器刷新回调"""
        self._emit_status_changed()

    def _emit_status_changed(self):
        """发出状态变更信号"""
        self.status_changed.emit()

    def _spawn_git(self, args: list, stdin=None):
        """启动一个 git 子进程并登记到 _active_procs。

        使用 start_new_session=True 让 git 及其子进程（ssh / credential helper）
        进入独立进程组，便于在取消时通过 killpg 一次性结束整棵进程树。

        Args:
            stdin: 传给 Popen 的 stdin（如 subprocess.PIPE，用于喂 patch 等输入）
        """
        env = dict(os.environ)
        env['GIT_TERMINAL_PROMPT'] = '0'
        if self._proxy:
            env['HTTPS_PROXY'] = self._proxy
            env['HTTP_PROXY'] = self._proxy
            env['ALL_PROXY'] = self._proxy
            # 小写变体：部分库（如 libcurl）也会读
            env['https_proxy'] = self._proxy
            env['http_proxy'] = self._proxy
            env['all_proxy'] = self._proxy
        proc = subprocess.Popen(
            ['git'] + list(args),
            cwd=self._repo_path,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # git 的输出（diff 内容、路径等）是 UTF-8；Windows 中文系统的默认
            # locale 编码是 cp936/GBK，不显式指定会在 diff 含中文时抛
            # UnicodeDecodeError（表现为生成提交信息拿不到 diff、查看 diff 报错）
            encoding='utf-8',
            errors='replace',
            env=env,
            start_new_session=True,
            creationflags=SUBPROCESS_FLAGS,
        )
        with self._proc_lock:
            self._active_procs.add(proc)
        return proc

    def _kill_proc(self, proc: subprocess.Popen):
        """SIGTERM 整个进程组；失败回落到 proc.terminate()。"""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def _release_proc(self, proc: subprocess.Popen):
        with self._proc_lock:
            self._active_procs.discard(proc)

    def cancel_running(self):
        """终止所有正在运行的 git 子进程（关闭程序或用户取消时调用）。"""
        with self._proc_lock:
            procs = list(self._active_procs)
        for p in procs:
            self._kill_proc(p)
        # 简短等待让进程真正退出；不长等，因为调用者通常急着关窗口
        for p in procs:
            try:
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _run_git(self, *args, check: bool = True, timeout: int = 30,
                 input_text: str = None) -> Tuple[bool, str]:
        """执行 git 命令

        Args:
            *args: git 命令参数
            check: 是否检查返回码
            timeout: 超时秒数（push/pull 等网络操作需要更长）
            input_text: 通过 stdin 喂给 git 的文本（如 `git apply -` 的 patch）

        Returns:
            (成功与否, 输出内容)
        """
        if not self._repo_path:
            return False, t("git_mgr.no_repo_path")

        proc = None
        try:
            proc = self._spawn_git(
                list(args),
                stdin=subprocess.PIPE if input_text is not None else None,
            )
            try:
                stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_proc(proc)
                try:
                    proc.communicate(timeout=3)
                except Exception:
                    pass
                return False, t("git_mgr.timeout")

            if check and proc.returncode != 0:
                return False, (stderr or '').strip() or (stdout or '').strip()
            return True, stdout
        except Exception as e:
            return False, str(e)
        finally:
            if proc is not None:
                self._release_proc(proc)

    def get_status(self) -> Tuple[List[GitFile], List[GitFile]]:
        """获取文件状态

        Returns:
            (已暂存文件列表, 未暂存文件列表)
        """
        staged: List[GitFile] = []
        unstaged: List[GitFile] = []

        if not self._repo_path:
            return staged, unstaged

        success, output = self._run_git('status', '--porcelain=v1', '-uall')
        if not success:
            self.error_occurred.emit(t("git_mgr.status_failed", error=output))
            return staged, unstaged

        for line in output.splitlines():
            if len(line) < 3:
                continue

            index_status = line[0]    # 暂存区状态
            worktree_status = line[1]  # 工作区状态
            path = line[3:]            # 文件路径

            # 处理重命名（格式：R  old_path -> new_path）
            old_path = None
            if ' -> ' in path:
                old_path, path = path.split(' -> ', 1)

            # 合并冲突（porcelain 的 XY 组合：UU/AA/DD/AU/UA/DU/UD）：
            # 归入"未暂存"列表并打上 is_conflict 标记，由 UI 显著提示，
            # 不再拆成 暂存+未暂存 两条让人困惑的记录。
            if (index_status + worktree_status) in ('UU', 'AA', 'DD', 'AU', 'UA', 'DU', 'UD'):
                unstaged.append(GitFile(
                    path=path, status=FileStatus.UNMERGED,
                    old_path=old_path, is_conflict=True,
                ))
                continue

            # 暂存区有变更
            if index_status != ' ' and index_status != '?':
                status = self._parse_status(index_status)
                if status:
                    staged.append(GitFile(path=path, status=status, old_path=old_path))

            # 工作区有变更或未跟踪
            if worktree_status != ' ':
                status = self._parse_status(worktree_status)
                if status:
                    unstaged.append(GitFile(path=path, status=status, old_path=old_path))

        return staged, unstaged

    def _parse_status(self, char: str) -> Optional[FileStatus]:
        """解析状态字符

        Args:
            char: 状态字符

        Returns:
            FileStatus 枚举值
        """
        mapping = {
            'M': FileStatus.MODIFIED,
            'A': FileStatus.ADDED,
            'D': FileStatus.DELETED,
            'R': FileStatus.RENAMED,
            'C': FileStatus.COPIED,
            '?': FileStatus.UNTRACKED,
            '!': FileStatus.IGNORED,
            'U': FileStatus.UNMERGED,
        }
        return mapping.get(char)

    def stage_file(self, path: str) -> bool:
        """暂存文件

        Args:
            path: 文件路径

        Returns:
            是否成功
        """
        success, output = self._run_git('add', '--', path)
        if not success:
            self.error_occurred.emit(t("git_mgr.stage_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def unstage_file(self, path: str) -> bool:
        """取消暂存文件

        Args:
            path: 文件路径

        Returns:
            是否成功
        """
        success, output = self._run_git('reset', 'HEAD', '--', path)
        if not success:
            self.error_occurred.emit(t("git_mgr.unstage_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def apply_patch(self, patch_text: str, cached: bool = True,
                    reverse: bool = False) -> bool:
        """对暂存区（index）应用一段 patch，用于 hunk 级暂存/取消暂存。

        - 暂存某个 hunk：patch 取自 `git diff`（index→worktree），正向 apply --cached
        - 取消暂存某个 hunk：patch 取自 `git diff --cached`（HEAD→index），
          反向（-R）apply --cached

        Args:
            patch_text: 完整可独立 apply 的 patch 文本（文件头 + 单个 hunk）
            cached: 是否作用于暂存区（--cached）
            reverse: 是否反向应用（-R）

        Returns:
            是否成功
        """
        if not patch_text or not patch_text.strip():
            return False
        args = ['apply']
        if cached:
            args.append('--cached')
        if reverse:
            args.append('-R')
        args += ['--unidiff-zero', '-']
        if not patch_text.endswith('\n'):
            patch_text += '\n'
        success, output = self._run_git(*args, input_text=patch_text)
        if not success:
            self.error_occurred.emit(t("git_mgr.apply_patch_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def stage_all(self) -> bool:
        """暂存所有文件

        Returns:
            是否成功
        """
        success, output = self._run_git('add', '-A')
        if not success:
            self.error_occurred.emit(t("git_mgr.stage_all_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def unstage_all(self) -> bool:
        """取消暂存所有文件

        Returns:
            是否成功
        """
        success, output = self._run_git('reset', 'HEAD')
        if not success:
            self.error_occurred.emit(t("git_mgr.unstage_all_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def discard_changes(self, path: str) -> bool:
        """放弃文件更改

        Args:
            path: 文件路径

        Returns:
            是否成功
        """
        # 先检查文件是否为未跟踪文件
        success, output = self._run_git('ls-files', '--error-unmatch', '--', path, check=False)

        if not success:
            # 未跟踪文件，直接删除（with path traversal protection）
            try:
                full_path = os.path.realpath(os.path.join(self._repo_path, path))
                repo_real = os.path.realpath(self._repo_path)
                if not full_path.startswith(repo_real + os.sep) and full_path != repo_real:
                    self.error_occurred.emit(t("git_mgr.delete_failed", error="Path outside repository"))
                    return False
                if os.path.exists(full_path):
                    os.remove(full_path)
                self.status_changed.emit()
                return True
            except Exception as e:
                self.error_occurred.emit(t("git_mgr.delete_failed", error=str(e)))
                return False
        else:
            # 已跟踪文件，使用 checkout
            success, output = self._run_git('checkout', '--', path)
            if not success:
                self.error_occurred.emit(t("git_mgr.discard_failed", error=output))
                return False
            self.status_changed.emit()
            return True

    def commit(self, message: str) -> bool:
        """提交暂存的更改

        Args:
            message: 提交信息

        Returns:
            是否成功
        """
        if not message.strip():
            self.error_occurred.emit(t("git_mgr.commit_empty"))
            return False

        # 存在未解决的合并冲突时拒绝提交，给出明确提示
        # （git 自身也会拒绝，但报错晦涩且夹带英文路径列表）。
        conflicts = self.get_conflict_files()
        if conflicts:
            self.error_occurred.emit(t("git_mgr.commit_conflicts", n=len(conflicts)))
            return False

        success, output = self._run_git('commit', '-m', message)
        if not success:
            self.error_occurred.emit(t("git_mgr.commit_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    # ---------- 合并冲突 ----------

    def get_conflict_files(self) -> List[str]:
        """获取当前未解决冲突的文件列表（git diff --diff-filter=U）。"""
        success, output = self._run_git('diff', '--name-only', '--diff-filter=U')
        if not success:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def is_merging(self) -> bool:
        """仓库是否处于合并中（.git/MERGE_HEAD 存在）。"""
        if not self._repo_path:
            return False
        git_dir = self._find_git_dir(self._repo_path)
        if not git_dir:
            return False
        return os.path.exists(os.path.join(git_dir, 'MERGE_HEAD'))

    def merge_abort(self) -> bool:
        """中止合并（git merge --abort），回到合并前状态。

        Returns:
            是否成功
        """
        success, output = self._run_git('merge', '--abort')
        if not success:
            self.error_occurred.emit(t("git_mgr.merge_abort_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def resolve_conflict_with(self, path: str, side: str) -> bool:
        """对冲突文件整体采用某一方版本并标记为已解决。

        Args:
            path: 文件路径
            side: 'ours'（我方/当前分支） 或 'theirs'（对方/被合并分支）

        Returns:
            是否成功

        注意：一方删除的冲突（DU/UD 等）checkout --ours/--theirs 可能失败，
        此时把 git 的报错原样抛给用户，由其在终端处理。
        """
        if side not in ('ours', 'theirs'):
            return False
        success, output = self._run_git('checkout', f'--{side}', '--', path)
        if not success:
            self.error_occurred.emit(t("git_mgr.resolve_failed", error=output))
            return False
        success, output = self._run_git('add', '--', path)
        if not success:
            self.error_occurred.emit(t("git_mgr.resolve_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    # ---------- Stash ----------

    def stash_save(self, message: str = '') -> Tuple[bool, str]:
        """贮藏当前修改（含未跟踪文件）。

        Args:
            message: stash 说明，可为空

        Returns:
            (是否成功, git 输出)。没有可贮藏的修改时 git 返回成功并输出
            "No local changes to save"，由调用方据此提示用户。
        """
        args = ['stash', 'push', '--include-untracked']
        message = (message or '').strip()
        if message:
            args += ['-m', message]
        success, output = self._run_git(*args)
        if not success:
            self.error_occurred.emit(t("git_mgr.stash_save_failed", error=output))
            return False, output
        self.status_changed.emit()
        return True, output

    def stash_list(self) -> List[GitStash]:
        """获取 stash 列表（新→旧，index 即 stash@{N} 中的 N）。"""
        if not self._repo_path:
            return []
        sep = '\x1f'
        fmt = sep.join(['%gd', '%cr', '%gs'])
        success, output = self._run_git('stash', 'list', f'--format={fmt}')
        if not success:
            return []
        stashes: List[GitStash] = []
        for line in output.splitlines():
            parts = line.split(sep)
            if len(parts) < 3:
                continue
            ref, date, subject = parts[0].strip(), parts[1].strip(), parts[2].strip()
            m = re.match(r'stash@\{(\d+)\}', ref)
            if not m:
                continue
            # subject 形如 "WIP on main: 1234abc msg" 或 "On main: 自定义说明"
            branch, message = '', subject
            m2 = re.match(r'(?:WIP on|On) ([^:]+): (.*)$', subject)
            if m2:
                branch, message = m2.group(1).strip(), m2.group(2).strip()
            stashes.append(GitStash(
                index=int(m.group(1)), ref=ref,
                branch=branch, message=message, date=date,
            ))
        return stashes

    def _stash_op(self, op: str, index: int, error_key: str) -> bool:
        """对指定 stash 执行 pop/apply/drop。"""
        ref = f'stash@{{{int(index)}}}'
        success, output = self._run_git('stash', op, ref)
        if not success:
            self.error_occurred.emit(t(error_key, error=output))
            return False
        self.status_changed.emit()
        return True

    def stash_pop(self, index: int) -> bool:
        """应用并删除指定 stash（产生冲突时 git 会保留该 stash 并报错）。"""
        return self._stash_op('pop', index, "git_mgr.stash_pop_failed")

    def stash_apply(self, index: int) -> bool:
        """应用指定 stash（保留该 stash 不删除）。"""
        return self._stash_op('apply', index, "git_mgr.stash_apply_failed")

    def stash_drop(self, index: int) -> bool:
        """删除指定 stash（不可恢复）。"""
        return self._stash_op('drop', index, "git_mgr.stash_drop_failed")

    def revert_commit(self, commit_hash: str) -> bool:
        """撤销某次提交（git revert）：生成一个新提交来抵消该提交的改动。

        这是 push 之后也安全的「撤销」方式，不改写历史。
        若产生冲突则自动 abort 并报错，避免仓库卡在冲突状态（GUI 无冲突解决界面）。

        Args:
            commit_hash: 要撤销的提交 hash

        Returns:
            是否成功
        """
        if not commit_hash:
            return False
        success, output = self._run_git('revert', '--no-edit', commit_hash, check=False)
        if not success:
            # 冲突或其它失败：回滚到干净状态再报错
            self._run_git('revert', '--abort', check=False)
            self.error_occurred.emit(t("git_mgr.revert_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def reset_to_commit(self, commit_hash: str, mode: str = 'mixed') -> bool:
        """重置当前分支到某次提交（git reset --<mode>）。

        会改写本地历史，仅适合未 push 的提交。
        - soft：保留工作区与暂存区（撤销提交但保留改动为已暂存）
        - mixed：保留工作区，清空暂存区（默认）
        - hard：丢弃工作区改动（危险，不可恢复）

        Args:
            commit_hash: 目标提交 hash
            mode: 'soft' | 'mixed' | 'hard'

        Returns:
            是否成功
        """
        if not commit_hash:
            return False
        if mode not in ('soft', 'mixed', 'hard'):
            mode = 'mixed'
        success, output = self._run_git('reset', f'--{mode}', commit_hash, check=False)
        if not success:
            self.error_occurred.emit(t("git_mgr.reset_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def get_diff(self, path: str, staged: bool = False) -> str:
        """获取文件 diff

        Args:
            path: 文件路径
            staged: 是否获取暂存区的 diff

        Returns:
            diff 内容
        """
        if staged:
            success, output = self._run_git('diff', '--cached', '--', path)
        else:
            success, output = self._run_git('diff', '--', path)

        if not success:
            return t("git_mgr.diff_failed", error=output)

        return output

    def get_current_branch(self) -> str:
        """获取当前分支名

        Returns:
            分支名（detached HEAD 时返回短 hash）
        """
        success, output = self._run_git('rev-parse', '--abbrev-ref', 'HEAD')
        if success:
            branch = output.strip()
            if branch == "HEAD":
                # Detached HEAD — return short commit hash instead
                ok, short = self._run_git('rev-parse', '--short', 'HEAD')
                if ok:
                    return f"(detached {short.strip()})"
            return branch
        return "unknown"

    def is_detached_head(self) -> bool:
        """Check if HEAD is detached."""
        success, output = self._run_git('rev-parse', '--abbrev-ref', 'HEAD')
        return success and output.strip() == "HEAD"

    def get_head_ref(self) -> Tuple[str, str]:
        """获取 HEAD 当前指向的引用

        Returns:
            (kind, name)
            - ('local',    branch_name)  HEAD 指向本地分支
            - ('tag',      tag_name)     detached 且 HEAD 正好等于某个 tag
            - ('detached', short_hash)   detached 且不匹配任何 tag
            - ('unknown',  '')           查询失败
        """
        success, output = self._run_git('symbolic-ref', '--quiet', '--short', 'HEAD')
        if success and output.strip():
            return ('local', output.strip())
        # detached：先看是否正好落在某个 tag 上
        ok, tag = self._run_git('describe', '--tags', '--exact-match', 'HEAD')
        if ok and tag.strip():
            return ('tag', tag.strip())
        ok, short = self._run_git('rev-parse', '--short', 'HEAD')
        if ok:
            return ('detached', short.strip())
        return ('unknown', '')

    def get_branches(self) -> List[GitBranch]:
        """获取所有分支

        Returns:
            分支列表
        """
        branches: List[GitBranch] = []

        # 获取本地分支
        success, output = self._run_git('branch', '-l')
        if success:
            for line in output.splitlines():
                is_current = line.startswith('*')
                name = line[2:].strip()
                # 跳过 detached HEAD 占位行，如 "(HEAD detached at abc1234)"
                if name and not name.startswith('('):
                    branches.append(GitBranch(name=name, is_current=is_current, is_remote=False))

        # 获取远程分支
        success, output = self._run_git('branch', '-r')
        if success:
            for line in output.splitlines():
                name = line.strip()
                if name and '->' not in name:  # 跳过 HEAD 指向
                    branches.append(GitBranch(name=name, is_current=False, is_remote=True))

        return branches

    def get_tags(self) -> List[str]:
        """获取所有 tag 名

        Returns:
            tag 名列表（按 creatordate 倒序）
        """
        success, output = self._run_git('tag', '--list', '--sort=-creatordate')
        if not success:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def checkout_branch(self, name: str) -> bool:
        """切换分支

        Args:
            name: 分支名

        Returns:
            是否成功
        """
        success, output = self._run_git('checkout', name)
        if not success:
            self.error_occurred.emit(t("git_mgr.checkout_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def create_branch(self, name: str, start_point: Optional[str] = None, checkout: bool = True) -> bool:
        """创建新分支（默认从当前 HEAD 创建并切换过去）。

        Args:
            name: 新分支名
            start_point: 起点引用（默认 = 当前 HEAD）
            checkout: True 时使用 'checkout -b'（创建+切换），False 时只用 'branch' 创建

        Returns:
            是否成功
        """
        name = (name or '').strip()
        if not name:
            self.error_occurred.emit(t("git_mgr.create_branch_failed", error=t("git_mgr.branch_name_empty")))
            return False
        if checkout:
            args = ['checkout', '-b', name]
        else:
            args = ['branch', name]
        if start_point:
            args.append(start_point)
        success, output = self._run_git(*args)
        if not success:
            self.error_occurred.emit(t("git_mgr.create_branch_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def delete_branch(self, name: str, force: bool = False) -> Tuple[bool, str]:
        """删除本地分支。

        Args:
            name: 分支名
            force: True 时使用 -D 强制删除（即使未合并）

        Returns:
            (是否成功, 错误/输出文本)。失败时调用方可据此判断是否
            需要给用户提供"force 删除"的二次确认（git 在未合并分支上
            的报错包含 'not fully merged'）。
        """
        name = (name or '').strip()
        if not name:
            return False, t("git_mgr.branch_name_empty")
        flag = '-D' if force else '-d'
        success, output = self._run_git('branch', flag, name)
        if not success:
            return False, output
        self.status_changed.emit()
        return True, output

    def checkout_ref(self, kind: str, name: str) -> bool:
        """切换到指定引用（本地分支 / 远程分支 / tag）

        Args:
            kind: 'local' | 'remote' | 'tag'
            name: 引用名（远程分支形如 'origin/feat'；tag 为 tag 名）

        Returns:
            是否成功
        """
        if kind == 'local':
            args = (name,)
        elif kind == 'remote':
            # origin/feat → 优先用 dwim 在本地建立跟踪分支；若已存在则直接切换
            local_name = name.split('/', 1)[1] if '/' in name else name
            success, output = self._run_git('checkout', local_name)
            if success:
                self.status_changed.emit()
                return True
            # 兜底：直接 checkout 远程引用（detached）
            args = (name,)
        elif kind == 'tag':
            # checkout tag 会进入 detached HEAD，使用 refs/tags/ 路径避免与同名分支歧义
            args = (f'refs/tags/{name}',)
        else:
            self.error_occurred.emit(t("git_mgr.checkout_failed", error=f"unknown ref kind: {kind}"))
            return False

        success, output = self._run_git('checkout', *args)
        if not success:
            self.error_occurred.emit(t("git_mgr.checkout_failed", error=output))
            return False
        self.status_changed.emit()
        return True

    def get_recent_commits(self, count: int = 10) -> List[GitCommit]:
        """获取最近的提交记录

        Args:
            count: 获取数量

        Returns:
            提交记录列表
        """
        commits: List[GitCommit] = []

        # 使用 %x00 作为分隔符
        format_str = '%H%x00%h%x00%an%x00%ad%x00%s'
        success, output = self._run_git('log', f'-{count}', f'--format={format_str}', '--date=short')

        if not success:
            return commits

        for line in output.strip().splitlines():
            parts = line.split('\x00')
            if len(parts) >= 5:
                commits.append(GitCommit(
                    hash=parts[0],
                    short_hash=parts[1],
                    author=parts[2],
                    date=parts[3],
                    message=parts[4]
                ))

        return commits

    @property
    def repo_path(self) -> Optional[str]:
        """获取仓库路径"""
        return self._repo_path

    def is_valid_repo(self) -> bool:
        """检查是否为有效的 Git 仓库"""
        return self._repo_path is not None

    def push(self, remote: str = "origin", branch: str = None) -> bool:
        """推送到远程仓库

        Args:
            remote: 远程仓库名称
            branch: 分支名，默认为当前分支

        Returns:
            是否成功
        """
        if self.is_detached_head():
            self.error_occurred.emit(t("git_mgr.push_failed", error="Cannot push in detached HEAD state"))
            return False

        if branch is None:
            branch = self.get_current_branch()

        success, output = self._run_git('push', remote, branch, timeout=GIT_NETWORK_TIMEOUT)
        if not success:
            self.error_occurred.emit(t("git_mgr.push_failed", error=output))
            return False
        return True

    def _run_git_verbose(self, *args, timeout: int = GIT_NETWORK_TIMEOUT) -> Tuple[bool, str]:
        """跑 git 命令并返回 (成功, 合并输出)。

        push/pull 的有用信息分散在 stdout（合并摘要/diffstat）和 stderr
        （远程进度、来自 URL、ref 更新），这里把两者合起来按自然顺序返回，
        便于直接展示给用户。
        """
        if not self._repo_path:
            return False, t("git_mgr.no_repo_path")
        proc = None
        try:
            proc = self._spawn_git(list(args))
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_proc(proc)
                try:
                    proc.communicate(timeout=3)
                except Exception:
                    pass
                return False, t("git_mgr.timeout")
            err = (stderr or '').strip()
            out = (stdout or '').strip()
            combined = "\n".join(p for p in (err, out) if p)
            return proc.returncode == 0, combined
        except Exception as e:
            return False, str(e)
        finally:
            if proc is not None:
                self._release_proc(proc)

    def fetch(self, remote: str = "origin") -> bool:
        """从远程抓取最新 refs（更新 origin/*），用于计算"落后多少条可 pull"。

        只更新远程跟踪分支，不改动工作区。后台静默调用：失败（如离线）时不报错，
        保留上次的计数即可。
        """
        success, _ = self._run_git('fetch', remote, '--quiet', timeout=GIT_NETWORK_TIMEOUT)
        return success

    def pull(self, remote: str = "origin", branch: str = None) -> bool:
        """从远程仓库拉取

        Args:
            remote: 远程仓库名称
            branch: 分支名，默认为当前分支

        Returns:
            是否成功
        """
        if self.is_detached_head():
            self.error_occurred.emit(t("git_mgr.pull_failed", error="Cannot pull in detached HEAD state"))
            return False

        if branch is None:
            branch = self.get_current_branch()

        success, output = self._run_git_verbose('pull', remote, branch, timeout=GIT_NETWORK_TIMEOUT)
        if not success:
            self.error_occurred.emit(t("git_mgr.pull_failed", error=output))
            return False
        # 把 pull 的完整输出（进度 + fast-forward + 文件统计）抛给 UI 展示
        self.op_output.emit('pull', output)
        self.status_changed.emit()
        return True

    def get_ahead_behind(self) -> tuple:
        """获取本地与远程的提交差异数

        Returns:
            (ahead_count, behind_count) 领先和落后的提交数
        """
        success, output = self._run_git('rev-list', '--left-right', '--count', '@{upstream}...HEAD', check=False)
        if not success:
            return (0, 0)

        parts = output.strip().split()
        if len(parts) == 2:
            try:
                return (int(parts[1]), int(parts[0]))  # ahead, behind
            except ValueError:
                pass
        return (0, 0)

    def get_log(self, limit: int = 150, all_branches: bool = True) -> List[dict]:
        """获取提交历史（含父提交，用于画 graph）。

        返回 [{hash, short, parents:[...], author, subject, refs:[...]}, ...]，
        按 --date-order 排列（新→旧）。
        """
        if not self._repo_path:
            return []
        sep = '\x1f'
        fmt = sep.join(['%H', '%P', '%an', '%s', '%D'])
        args = ['log', '--date-order', f'--pretty=format:{fmt}', f'-n{limit}']
        if all_branches:
            args.insert(1, '--all')
        ok, out = self._run_git(*args, check=False)
        if not ok:
            return []
        commits: List[dict] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split(sep)
            if len(parts) < 5:
                continue
            h, parents, author, subject, refs = parts[:5]
            commits.append({
                'hash': h,
                'short': h[:7],
                'parents': parents.split() if parents.strip() else [],
                'author': author,
                'subject': subject,
                'refs': [r.strip() for r in refs.split(',') if r.strip()],
            })
        return commits

    def get_commit_show(self, commit_hash: str) -> str:
        """获取某次提交的详情（摘要 + 改动），用于点击 graph 时展示。"""
        ok, out = self._run_git(
            'show', commit_hash, '--stat', '--patch',
            '--format=commit %H%nAuthor: %an <%ae>%nDate:   %ad%n%n    %s%n%n%b',
            check=False,
        )
        # check=False 时 _run_git 失败会把错误信息放进 out，直接回传即可
        return out

    def refresh(self):
        """手动刷新状态"""
        self.status_changed.emit()
