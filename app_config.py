"""共享主配置（.smart_terminal_config.json）的单点读写。

多进程（多窗口）共用这一份文件。历史上各组件手抄「读-改-写」三部曲
（read_config_json → ok 检查 → mutate → atomic_write_json），任何一处漏掉
ok 检查或用非原子写，就会把其它进程维护的字段清空（git_proxy 清零事故）。
本模块把三部曲收敛到一处，并补上两块短板：

- 进程间文件锁：原子写只保证「读到的不是半截」，两个进程同时读-改-写
  仍会后写覆盖先写、丢掉先写者的更改。锁把整个读-改-写窗口串行化。
- 写失败不再静默：记录 warning 日志并返回 False，调用方可提示用户
  （磁盘满/权限问题时旧代码 `except: pass`，全部设置无声丢失）。

用法：
    from app_config import read_config, update_config, update_config_with

    cfg = read_config()                        # 容错读：缺失/损坏返回 {}
    update_config({'theme': 'dark'})           # patch 合并写
    update_config_with(lambda c: c.pop('k', None))   # 复杂修改（删键/嵌套）
"""
import threading
from contextlib import contextmanager
from pathlib import Path

from app_logging import get_logger
from utils import get_config_path, read_config_json, atomic_write_json

logger = get_logger(__name__)

# 进程内锁：同进程多线程（GUI + 各类 worker）也要串行化读-改-写
_process_lock = threading.Lock()


def _lock_file_path() -> Path:
    # 独立锁文件：不能锁配置文件本身——原子写靠 rename 换 inode，锁会失效
    return get_config_path().with_name('.smart_terminal_config.lock')


@contextmanager
def _inter_process_lock():
    """跨进程文件锁（POSIX 用 flock；Windows 用 msvcrt.locking）。

    平台不支持或加锁失败时退化为仅进程内锁——不比旧行为差，
    原子写仍保证不会出现半截文件。
    """
    handle = None
    locked = False
    try:
        try:
            lock_path = _lock_file_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, 'a+')
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            except ImportError:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
        except Exception as e:
            logger.debug(f"config lock unavailable, in-process lock only: {e}")
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    try:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except ImportError:
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            try:
                handle.close()
            except Exception:
                pass


def read_config() -> dict:
    """容错读取整份配置：文件缺失或损坏返回 {}。

    只读场景不需要锁——原子写保证读到的永远是完整文件。
    """
    cfg, _ok = read_config_json(get_config_path())
    return cfg


def update_config_with(mutator, *, description: str = '') -> bool:
    """在锁内执行 读 → mutator(cfg) 原地修改 → 原子写，返回是否成功。

    - mutator 返回 False 表示「无需写入」（返回 None/True 均照常写）。
    - 读到损坏文件时放弃写入（保持既有语义：宁可这次不存，也不能把
      别的进程维护的字段当"损坏"覆盖掉）。有锁之后这基本只剩真损坏。
    - description 用于日志定位是哪个组件的写入失败。
    """
    path = get_config_path()
    with _process_lock, _inter_process_lock():
        cfg, ok = read_config_json(path)
        if not ok:
            logger.warning(f"config unreadable, skipped save ({description or 'unknown'})")
            return False
        try:
            if mutator(cfg) is False:
                return True  # 调用方判定无变化，跳过写入
        except Exception as e:
            logger.warning(f"config mutator failed ({description or 'unknown'}): {e}")
            return False
        if atomic_write_json(path, cfg):
            return True
        logger.warning(f"config write failed ({description or 'unknown'}) — disk full or permission?")
        return False


def update_config(patch: dict, *, description: str = '') -> bool:
    """把 patch 合并进配置并写盘（最常用入口），返回是否成功。"""
    if not patch:
        return True

    def _apply(cfg):
        cfg.update(patch)

    return update_config_with(_apply, description=description)
