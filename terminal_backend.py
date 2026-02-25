"""
跨平台终端后端抽象层
支持 Unix (pty/fork) 和 Windows (pywinpty)
"""
import os
import sys
import threading
from abc import ABC, abstractmethod
from typing import Optional, List, Callable

# 平台检测
IS_WINDOWS = sys.platform == 'win32'


class TerminalBackend(ABC):
    """终端后端抽象基类"""

    def __init__(self):
        self.on_output: Optional[Callable[[bytes], None]] = None
        self.on_exit: Optional[Callable[[int], None]] = None
        self._running = False

    @abstractmethod
    def start(self, command: List[str], cwd: Optional[str] = None,
              cols: int = 80, rows: int = 24) -> bool:
        """启动终端进程

        Args:
            command: 要执行的命令
            cwd: 工作目录
            cols: 终端列数
            rows: 终端行数

        Returns:
            是否成功启动
        """
        pass

    @abstractmethod
    def write(self, data: bytes) -> bool:
        """写入数据到终端

        Args:
            data: 要写入的字节数据

        Returns:
            是否成功写入
        """
        pass

    @abstractmethod
    def resize(self, cols: int, rows: int) -> bool:
        """调整终端大小

        Args:
            cols: 新的列数
            rows: 新的行数

        Returns:
            是否成功调整
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """停止终端进程"""
        pass

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """检查进程是否在运行"""
        pass


if IS_WINDOWS:
    import winpty

    class WindowsBackend(TerminalBackend):
        """Windows 终端后端 - 使用 pywinpty"""

        def __init__(self):
            super().__init__()
            self._pty: Optional[winpty.PTY] = None
            self._reader_thread: Optional[threading.Thread] = None
            self._process_handle = None

        def start(self, command: List[str], cwd: Optional[str] = None,
                  cols: int = 80, rows: int = 24) -> bool:
            if self._pty is not None:
                return False

            try:
                # 创建 PTY
                self._pty = winpty.PTY(cols, rows)

                # 构建命令字符串
                if len(command) == 1:
                    cmd_str = command[0]
                else:
                    # 处理带参数的命令
                    cmd_str = ' '.join(command)

                # 设置工作目录
                if cwd is None:
                    cwd = os.getcwd()

                # 启动进程
                self._pty.spawn(cmd_str, cwd=cwd)
                self._running = True

                # 启动读取线程
                self._reader_thread = threading.Thread(
                    target=self._read_loop,
                    daemon=True
                )
                self._reader_thread.start()

                return True

            except Exception as e:
                print(f"[WindowsBackend] Error starting process: {e}")
                self._cleanup()
                return False

        def _read_loop(self):
            """后台读取循环"""
            while self._running and self._pty is not None:
                try:
                    # 读取输出 (阻塞读取，有超时)
                    data = self._pty.read(65536, blocking=False)
                    if data:
                        if self.on_output:
                            self.on_output(data.encode('utf-8') if isinstance(data, str) else data)
                    else:
                        # 短暂休眠避免 CPU 空转
                        import time
                        time.sleep(0.01)

                    # 检查进程是否还在运行
                    if not self._pty.isalive():
                        self._running = False
                        if self.on_exit:
                            self.on_exit(0)
                        break

                except Exception as e:
                    if self._running:
                        print(f"[WindowsBackend] Read error: {e}")
                    break

        def write(self, data: bytes) -> bool:
            if self._pty is None:
                return False
            try:
                # pywinpty 接受字符串
                text = data.decode('utf-8', errors='replace')
                self._pty.write(text)
                return True
            except Exception as e:
                print(f"[WindowsBackend] Write error: {e}")
                return False

        def resize(self, cols: int, rows: int) -> bool:
            if self._pty is None:
                return False
            try:
                self._pty.set_size(cols, rows)
                return True
            except Exception as e:
                print(f"[WindowsBackend] Resize error: {e}")
                return False

        def stop(self) -> None:
            self._running = False

            # 清理回调引用（打破循环引用）
            self.on_output = None
            self.on_exit = None

            self._cleanup()

        def _cleanup(self):
            """清理资源"""
            # 先等待线程退出
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=2.0)
                self._reader_thread = None

            # 然后关闭 PTY
            if self._pty is not None:
                try:
                    self._pty.close()
                except:
                    pass
                self._pty = None

        @property
        def is_running(self) -> bool:
            return self._running and self._pty is not None and self._pty.isalive()

else:
    # Unix 系统
    import pty
    import select
    import signal
    import struct
    import fcntl
    import termios

    class UnixBackend(TerminalBackend):
        """Unix 终端后端 - 使用 pty/fork"""

        def __init__(self):
            super().__init__()
            self._master_fd: Optional[int] = None
            self._child_pid: Optional[int] = None
            self._reader_thread: Optional[threading.Thread] = None
            self._cols = 80
            self._rows = 24

        def start(self, command: List[str], cwd: Optional[str] = None,
                  cols: int = 80, rows: int = 24) -> bool:
            if self._child_pid is not None:
                return False

            self._cols = cols
            self._rows = rows

            try:
                # 创建 PTY
                self._master_fd, slave_fd = pty.openpty()

                # 设置 PTY 大小
                self._set_pty_size(slave_fd, cols, rows)

                # Fork
                self._child_pid = os.fork()

                if self._child_pid == 0:
                    # 子进程
                    os.close(self._master_fd)
                    os.setsid()

                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

                    os.dup2(slave_fd, 0)
                    os.dup2(slave_fd, 1)
                    os.dup2(slave_fd, 2)

                    if slave_fd > 2:
                        os.close(slave_fd)

                    # 切换工作目录
                    if cwd and os.path.isdir(cwd):
                        os.chdir(cwd)

                    # 设置环境变量
                    env = os.environ.copy()
                    env['TERM'] = 'xterm-256color'
                    env['COLORTERM'] = 'truecolor'
                    env['COLUMNS'] = str(cols)
                    env['LINES'] = str(rows)

                    # 确保 UTF-8 编码支持（修复中文等 Unicode 字符显示乱码问题）
                    if 'LANG' not in env or 'UTF-8' not in env.get('LANG', ''):
                        env['LANG'] = 'en_US.UTF-8'
                    if 'LC_ALL' not in env:
                        env['LC_ALL'] = env['LANG']

                    # 清除 macOS Terminal.app 的会话恢复相关环境变量
                    # 这些变量会导致 zsh 尝试恢复之前的会话状态，
                    # 产生 "Restored session" 消息和控制字符乱码
                    session_vars = [
                        'TERM_SESSION_ID',           # Terminal.app 会话ID
                        'SHELL_SESSION_DID_INIT',    # Shell 会话已初始化标记
                        'SHELL_SESSION_FILE',        # Shell 会话文件路径
                        'SHELL_SESSION_HISTFILE',    # Shell 会话历史文件
                        'SHELL_SESSION_HISTFILE_NEW', # 新会话历史文件
                        'SHELL_SESSION_HISTORY',     # Shell 会话历史
                        'SHELL_SESSION_DIR',         # Shell 会话目录
                        'SECURITYSESSIONID',         # Security 会话ID
                        'ITERM_SESSION_ID',          # iTerm2 会话ID（如果有）
                    ]
                    for var in session_vars:
                        env.pop(var, None)

                    os.execvpe(command[0], command, env)

                else:
                    # 父进程
                    os.close(slave_fd)
                    self._running = True

                    # 启动读取线程
                    self._reader_thread = threading.Thread(
                        target=self._read_loop,
                        daemon=True
                    )
                    self._reader_thread.start()

                    return True

            except Exception as e:
                print(f"[UnixBackend] Error starting process: {e}")
                self._cleanup()
                return False

            return False

        def _set_pty_size(self, fd: int, cols: int, rows: int):
            """设置 PTY 大小"""
            try:
                size = struct.pack('HHHH', rows, cols, 0, 0)
                fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
            except:
                pass

        def _read_loop(self):
            """后台读取循环"""
            check_counter = 0  # 用于降低 waitpid 调用频率
            while self._running:
                try:
                    ready, _, _ = select.select([self._master_fd], [], [], 0.1)
                    if ready:
                        try:
                            data = os.read(self._master_fd, 65536)
                            if data:
                                if self.on_output:
                                    self.on_output(data)
                            else:
                                # EOF - 进程已退出
                                break
                        except OSError:
                            break
                    else:
                        # 只在没有数据可读时检查子进程状态（大约每秒一次）
                        check_counter += 1
                        if check_counter >= 10 and self._child_pid:
                            check_counter = 0
                            try:
                                pid, status = os.waitpid(self._child_pid, os.WNOHANG)
                                if pid != 0:
                                    self._running = False
                                    if self.on_exit:
                                        self.on_exit(status)
                                    break
                            except ChildProcessError:
                                break

                except ChildProcessError:
                    break
                except Exception:
                    break

        def write(self, data: bytes) -> bool:
            if self._master_fd is None:
                return False
            try:
                os.write(self._master_fd, data)
                return True
            except Exception as e:
                print(f"[UnixBackend] Write error: {e}")
                return False

        def resize(self, cols: int, rows: int) -> bool:
            if self._master_fd is None:
                return False
            try:
                self._cols = cols
                self._rows = rows
                self._set_pty_size(self._master_fd, cols, rows)

                # 发送 SIGWINCH 信号
                if self._child_pid:
                    try:
                        os.killpg(os.getpgid(self._child_pid), signal.SIGWINCH)
                    except (ProcessLookupError, PermissionError):
                        try:
                            os.kill(self._child_pid, signal.SIGWINCH)
                        except ProcessLookupError:
                            pass
                return True
            except Exception as e:
                print(f"[UnixBackend] Resize error: {e}")
                return False

        def stop(self) -> None:
            self._running = False

            # 清理回调引用（打破循环引用）
            self.on_output = None
            self.on_exit = None

            # 发送终止信号
            if self._child_pid:
                try:
                    os.kill(self._child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

            self._cleanup()

        def _cleanup(self):
            """清理资源"""
            # 先等待线程退出（线程检测到 _running=False 会自行退出）
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=2.0)
                self._reader_thread = None

            # 然后关闭文件描述符
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except:
                    pass
                self._master_fd = None

            # 最后清理子进程引用
            self._child_pid = None

        @property
        def is_running(self) -> bool:
            return self._running and self._child_pid is not None


def create_backend() -> TerminalBackend:
    """创建适合当前平台的终端后端"""
    if IS_WINDOWS:
        return WindowsBackend()
    else:
        return UnixBackend()
