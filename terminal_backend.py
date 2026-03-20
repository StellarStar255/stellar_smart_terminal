"""
跨平台终端后端抽象层
支持 Unix (pty/fork) 和 Windows (ConPTY passthrough via ctypes)
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
    import ctypes
    import ctypes.wintypes as wintypes
    import subprocess

    # Windows API 常量
    PSEUDOCONSOLE_PASSTHROUGH = 0x0000008
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    STILL_ACTIVE = 259
    WAIT_TIMEOUT = 0x00000102
    INFINITE = 0xFFFFFFFF
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_FLAG_OVERLAPPED = 0x40000000

    # Windows API 结构体
    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class STARTUPINFOEX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    # Windows API 函数
    kernel32 = ctypes.windll.kernel32

    # CreatePipe
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),  # hReadPipe
        ctypes.POINTER(wintypes.HANDLE),  # hWritePipe
        ctypes.POINTER(SECURITY_ATTRIBUTES),  # lpPipeAttributes
        wintypes.DWORD,  # nSize
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL

    # CreatePseudoConsole
    kernel32.CreatePseudoConsole.argtypes = [
        COORD,           # size
        wintypes.HANDLE, # hInput
        wintypes.HANDLE, # hOutput
        wintypes.DWORD,  # dwFlags
        ctypes.POINTER(wintypes.HANDLE),  # phPC (HPCON*)
    ]
    kernel32.CreatePseudoConsole.restype = ctypes.HRESULT

    # ResizePseudoConsole
    kernel32.ResizePseudoConsole.argtypes = [
        wintypes.HANDLE,  # hPC
        COORD,            # size
    ]
    kernel32.ResizePseudoConsole.restype = ctypes.HRESULT

    # ClosePseudoConsole
    kernel32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
    kernel32.ClosePseudoConsole.restype = None

    # InitializeProcThreadAttributeList
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,  # lpAttributeList
        wintypes.DWORD,   # dwAttributeCount
        wintypes.DWORD,   # dwFlags
        ctypes.POINTER(ctypes.c_size_t),  # lpSize
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

    # UpdateProcThreadAttribute
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,   # lpAttributeList
        wintypes.DWORD,    # dwFlags
        ctypes.POINTER(wintypes.DWORD) if ctypes.sizeof(ctypes.c_void_p) == 4
            else ctypes.c_ulonglong,  # Attribute (DWORD_PTR)
        ctypes.c_void_p,   # lpValue
        ctypes.c_size_t,   # cbSize
        ctypes.c_void_p,   # lpPreviousValue
        ctypes.c_void_p,   # lpReturnSize
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

    # CreateProcessW
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,   # lpApplicationName
        wintypes.LPWSTR,    # lpCommandLine
        ctypes.c_void_p,    # lpProcessAttributes
        ctypes.c_void_p,    # lpThreadAttributes
        wintypes.BOOL,      # bInheritHandles
        wintypes.DWORD,     # dwCreationFlags
        ctypes.c_void_p,    # lpEnvironment
        wintypes.LPCWSTR,   # lpCurrentDirectory
        ctypes.c_void_p,    # lpStartupInfo (STARTUPINFOEX*)
        ctypes.POINTER(PROCESS_INFORMATION),  # lpProcessInformation
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL

    # ReadFile / WriteFile
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL

    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL

    # CloseHandle / GetExitCodeProcess / WaitForSingleObject
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    # DeleteProcThreadAttributeList
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None

    # GetLastError
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD

    class WindowsBackend(TerminalBackend):
        """Windows 终端后端 - 使用 ConPTY Passthrough 模式（与 Windows Terminal 相同）"""

        def __init__(self):
            super().__init__()
            self._hpc = None          # PseudoConsole handle
            self._h_process = None    # 子进程 handle
            self._h_thread = None     # 子进程主线程 handle
            self._pipe_in_write = None   # 写入管道（我们写入 → 子进程stdin）
            self._pipe_out_read = None   # 读取管道（子进程stdout → 我们读取）
            self._pipe_in_read = None    # 管道读取端（给 ConPTY）
            self._pipe_out_write = None  # 管道写入端（给 ConPTY）
            self._reader_thread: Optional[threading.Thread] = None
            self._attr_list_buf = None   # 保持 attribute list 内存存活

        def start(self, command: List[str], cwd: Optional[str] = None,
                  cols: int = 80, rows: int = 24) -> bool:
            if self._hpc is not None:
                return False

            try:
                # 设置终端环境变量
                os.environ.setdefault('TERM', 'xterm-256color')
                os.environ.setdefault('COLORTERM', 'truecolor')

                # 1. 创建管道
                pipe_in_read = wintypes.HANDLE()
                pipe_in_write = wintypes.HANDLE()
                pipe_out_read = wintypes.HANDLE()
                pipe_out_write = wintypes.HANDLE()

                if not kernel32.CreatePipe(
                    ctypes.byref(pipe_in_read),
                    ctypes.byref(pipe_in_write),
                    None, 0
                ):
                    raise OSError(f"CreatePipe (input) failed: {kernel32.GetLastError()}")

                if not kernel32.CreatePipe(
                    ctypes.byref(pipe_out_read),
                    ctypes.byref(pipe_out_write),
                    None, 0
                ):
                    kernel32.CloseHandle(pipe_in_read)
                    kernel32.CloseHandle(pipe_in_write)
                    raise OSError(f"CreatePipe (output) failed: {kernel32.GetLastError()}")

                self._pipe_in_read = pipe_in_read.value
                self._pipe_in_write = pipe_in_write.value
                self._pipe_out_read = pipe_out_read.value
                self._pipe_out_write = pipe_out_write.value

                # 2. 创建 PseudoConsole（使用 PASSTHROUGH 标志）
                size = COORD(cols, rows)
                hpc = wintypes.HANDLE()
                passthrough_ok = False
                hr = kernel32.CreatePseudoConsole(
                    size,
                    wintypes.HANDLE(self._pipe_in_read),
                    wintypes.HANDLE(self._pipe_out_write),
                    PSEUDOCONSOLE_PASSTHROUGH,
                    ctypes.byref(hpc),
                )
                if hr == 0:
                    passthrough_ok = True
                    print("[WindowsBackend] *** ConPTY PASSTHROUGH mode enabled ***")
                else:
                    # Passthrough 不可用，回退到普通模式
                    print(f"[WindowsBackend] *** Passthrough FAILED (hr=0x{hr:08x}), falling back to normal ConPTY ***")
                    hr = kernel32.CreatePseudoConsole(
                        size,
                        wintypes.HANDLE(self._pipe_in_read),
                        wintypes.HANDLE(self._pipe_out_write),
                        0,  # 无特殊标志
                        ctypes.byref(hpc),
                    )
                    if hr != 0:
                        raise OSError(f"CreatePseudoConsole failed: HRESULT 0x{hr:08x}")
                    print("[WindowsBackend] Normal ConPTY mode (no passthrough)")

                self._hpc = hpc.value

                # 3. 初始化进程线程属性列表
                attr_size = ctypes.c_size_t(0)
                kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))

                attr_list_buf = (ctypes.c_byte * attr_size.value)()
                self._attr_list_buf = attr_list_buf

                if not kernel32.InitializeProcThreadAttributeList(
                    attr_list_buf, 1, 0, ctypes.byref(attr_size)
                ):
                    raise OSError(f"InitializeProcThreadAttributeList failed: {kernel32.GetLastError()}")

                # 4. 设置 PSEUDOCONSOLE 属性
                if not kernel32.UpdateProcThreadAttribute(
                    attr_list_buf,
                    0,
                    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                    self._hpc,
                    ctypes.sizeof(wintypes.HANDLE),
                    None,
                    None,
                ):
                    raise OSError(f"UpdateProcThreadAttribute failed: {kernel32.GetLastError()}")

                # 5. 构建命令字符串
                if len(command) == 1:
                    cmd_str = command[0]
                else:
                    cmd_str = subprocess.list2cmdline(command)

                # 设置工作目录
                if cwd is None:
                    cwd = os.getcwd()

                # 6. 构建环境变量块（Unicode）
                env = os.environ.copy()
                # 显式设置终端类型和能力标识。
                # 在 Windows 上，Node.js CLI 工具（如 Claude Code/Ink）会检查多个
                # 环境变量来判断终端是否支持 Unicode、颜色、交互式渲染等。
                # 缺少这些变量会导致回退到精简布局（无 box-drawing 边框）。
                env['TERM'] = 'xterm-256color'
                env['COLORTERM'] = 'truecolor'
                env['FORCE_COLOR'] = '3'        # 强制 24-bit 颜色支持
                env['TERM_PROGRAM'] = 'vscode'   # 标识为已知支持完整渲染的终端
                env['TERM_PROGRAM_VERSION'] = '1.96.0'
                if 'WT_SESSION' not in env:
                    import uuid
                    env['WT_SESSION'] = str(uuid.uuid4())
                env_block = '\0'.join(f'{k}={v}' for k, v in env.items()) + '\0\0'

                # 7. 创建进程
                si = STARTUPINFOEX()
                si.cb = ctypes.sizeof(STARTUPINFOEX)
                si.lpAttributeList = ctypes.cast(attr_list_buf, ctypes.c_void_p).value

                pi = PROCESS_INFORMATION()

                cmd_buf = ctypes.create_unicode_buffer(cmd_str)
                env_buf = ctypes.create_unicode_buffer(env_block)

                if not kernel32.CreateProcessW(
                    None,           # lpApplicationName
                    cmd_buf,        # lpCommandLine
                    None,           # lpProcessAttributes
                    None,           # lpThreadAttributes
                    False,          # bInheritHandles
                    EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                    env_buf,        # lpEnvironment
                    cwd,            # lpCurrentDirectory
                    ctypes.byref(si),
                    ctypes.byref(pi),
                ):
                    raise OSError(f"CreateProcessW failed: {kernel32.GetLastError()}")

                self._h_process = pi.hProcess
                self._h_thread = pi.hThread

                # 清理属性列表（进程已创建，不再需要）
                kernel32.DeleteProcThreadAttributeList(attr_list_buf)

                # 关闭 ConPTY 端的管道端（我们不直接使用这些）
                kernel32.CloseHandle(wintypes.HANDLE(self._pipe_in_read))
                self._pipe_in_read = None
                kernel32.CloseHandle(wintypes.HANDLE(self._pipe_out_write))
                self._pipe_out_write = None

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
                import traceback
                traceback.print_exc()
                self._cleanup()
                return False

        def _read_loop(self):
            """后台读取循环 - 从 ConPTY 输出管道读取"""
            buf_size = 65536
            buf = ctypes.create_string_buffer(buf_size)
            bytes_read = wintypes.DWORD(0)

            while self._running and self._pipe_out_read is not None:
                try:
                    success = kernel32.ReadFile(
                        wintypes.HANDLE(self._pipe_out_read),
                        buf,
                        buf_size,
                        ctypes.byref(bytes_read),
                        None,
                    )
                    if success and bytes_read.value > 0:
                        data = buf.raw[:bytes_read.value]
                        if self.on_output:
                            self.on_output(data)
                    elif not success:
                        # 管道关闭（进程退出）
                        break
                except Exception as e:
                    if self._running:
                        print(f"[WindowsBackend] Read error: {e}")
                    break

            # 进程结束处理
            if self._running:
                self._running = False
                exit_code = self._get_exit_code()
                if self.on_exit:
                    self.on_exit(exit_code)

        def _get_exit_code(self) -> int:
            """获取子进程退出码"""
            if self._h_process is None:
                return -1
            code = wintypes.DWORD(0)
            kernel32.GetExitCodeProcess(
                wintypes.HANDLE(self._h_process),
                ctypes.byref(code)
            )
            return code.value

        def write(self, data: bytes) -> bool:
            if self._pipe_in_write is None:
                return False
            try:
                written = wintypes.DWORD(0)
                success = kernel32.WriteFile(
                    wintypes.HANDLE(self._pipe_in_write),
                    data,
                    len(data),
                    ctypes.byref(written),
                    None,
                )
                return bool(success)
            except Exception as e:
                print(f"[WindowsBackend] Write error: {e}")
                return False

        def resize(self, cols: int, rows: int) -> bool:
            if self._hpc is None:
                return False
            try:
                size = COORD(cols, rows)
                hr = kernel32.ResizePseudoConsole(
                    wintypes.HANDLE(self._hpc), size
                )
                return hr == 0
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
            """清理所有资源"""
            # 关闭 PseudoConsole（这会导致子进程收到关闭信号）
            if self._hpc is not None:
                try:
                    kernel32.ClosePseudoConsole(wintypes.HANDLE(self._hpc))
                except Exception:
                    pass
                self._hpc = None

            # 等待读取线程退出
            if self._reader_thread is not None:
                self._reader_thread.join(timeout=3.0)
                self._reader_thread = None

            # 关闭管道
            for pipe_attr in ('_pipe_in_write', '_pipe_in_read',
                              '_pipe_out_read', '_pipe_out_write'):
                handle = getattr(self, pipe_attr, None)
                if handle is not None:
                    try:
                        kernel32.CloseHandle(wintypes.HANDLE(handle))
                    except Exception:
                        pass
                    setattr(self, pipe_attr, None)

            # 关闭进程和线程 handles
            if self._h_thread is not None:
                try:
                    kernel32.CloseHandle(wintypes.HANDLE(self._h_thread))
                except Exception:
                    pass
                self._h_thread = None

            if self._h_process is not None:
                try:
                    kernel32.CloseHandle(wintypes.HANDLE(self._h_process))
                except Exception:
                    pass
                self._h_process = None

            self._attr_list_buf = None

        @property
        def is_running(self) -> bool:
            if not self._running or self._h_process is None:
                return False
            # 检查进程是否还在运行
            code = wintypes.DWORD(0)
            kernel32.GetExitCodeProcess(
                wintypes.HANDLE(self._h_process),
                ctypes.byref(code)
            )
            return code.value == STILL_ACTIVE

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
