"""
Git 管理器后端
提供 Git 仓库操作的核心功能
"""
import os
import subprocess
from enum import Enum
from dataclasses import dataclass
from typing import List, Tuple, Optional
from PyQt6.QtCore import QObject, pyqtSignal, QFileSystemWatcher, QTimer

from i18n import t


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

        # 获取仓库根目录
        repo_root = os.path.dirname(git_dir)

        # 如果是同一个仓库，不需要重新设置
        if self._repo_path == repo_root:
            return True

        self._repo_path = repo_root
        self._setup_watcher(git_dir)
        self._refresh_timer.start()
        return True

    def _find_git_dir(self, path: str) -> Optional[str]:
        """查找 .git 目录

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

    def _run_git(self, *args, check: bool = True) -> Tuple[bool, str]:
        """执行 git 命令

        Args:
            *args: git 命令参数
            check: 是否检查返回码

        Returns:
            (成功与否, 输出内容)
        """
        if not self._repo_path:
            return False, t("git_mgr.no_repo_path")

        try:
            result = subprocess.run(
                ['git'] + list(args),
                cwd=self._repo_path,
                capture_output=True,
                text=True,
                timeout=30
            )

            if check and result.returncode != 0:
                return False, result.stderr.strip() or result.stdout.strip()

            return True, result.stdout
        except subprocess.TimeoutExpired:
            return False, t("git_mgr.timeout")
        except Exception as e:
            return False, str(e)

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
                old_path, path = path.split(' -> ')

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
            # 未跟踪文件，直接删除
            try:
                full_path = os.path.join(self._repo_path, path)
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

        success, output = self._run_git('commit', '-m', message)
        if not success:
            self.error_occurred.emit(t("git_mgr.commit_failed", error=output))
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
            分支名
        """
        success, output = self._run_git('rev-parse', '--abbrev-ref', 'HEAD')
        if success:
            return output.strip()
        return "unknown"

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
                if name:
                    branches.append(GitBranch(name=name, is_current=is_current, is_remote=False))

        # 获取远程分支
        success, output = self._run_git('branch', '-r')
        if success:
            for line in output.splitlines():
                name = line.strip()
                if name and '->' not in name:  # 跳过 HEAD 指向
                    branches.append(GitBranch(name=name, is_current=False, is_remote=True))

        return branches

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
        if branch is None:
            branch = self.get_current_branch()

        success, output = self._run_git('push', remote, branch)
        if not success:
            self.error_occurred.emit(t("git_mgr.push_failed", error=output))
            return False
        return True

    def pull(self, remote: str = "origin", branch: str = None) -> bool:
        """从远程仓库拉取

        Args:
            remote: 远程仓库名称
            branch: 分支名，默认为当前分支

        Returns:
            是否成功
        """
        if branch is None:
            branch = self.get_current_branch()

        success, output = self._run_git('pull', remote, branch)
        if not success:
            self.error_occurred.emit(t("git_mgr.pull_failed", error=output))
            return False
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

    def refresh(self):
        """手动刷新状态"""
        self.status_changed.emit()
