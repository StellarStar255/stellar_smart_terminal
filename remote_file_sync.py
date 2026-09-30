"""Serialize editor uploads and publish worker results on the Qt thread."""
import os
import json
import time
import shutil
import tempfile

from file_persistence import atomic_writer
from utils import get_data_dir
from app_logging import get_logger
from i18n import t

logger = get_logger(__name__)

from PyQt6.QtCore import QObject, pyqtSignal


class RemoteFileSync(QObject):
    state_changed = pyqtSignal(str, str, str)
    _completed = pyqtSignal(str, str, str)

    def __init__(self, session_for, parent=None):
        super().__init__(parent)
        self._session_for = session_for
        self._targets = {}
        self._latest = {}
        self._active = {}
        self._states = {}
        self._completed.connect(self._finish)
        self._capture_failed = set()
        self._pending_dir = os.path.join(get_data_dir(), 'remote_pending')

    @staticmethod
    def _unlink(path):
        for candidate in (path, path + '.json'):
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning('Pending upload cleanup failed: %s', candidate, exc_info=True)

    def register(self, path, host, remote, session):
        self._targets[path] = (host, remote, session)
        self._states.setdefault(path, ('synced', ''))
        if path in self._latest or path in self._capture_failed:
            return
        # Recover the newest unsent snapshot after a restart. Never auto-upload it.
        candidates = []
        try:
            names = os.listdir(self._pending_dir)
        except OSError:
            return
        for name in names:
            if not name.endswith('.json'):
                continue
            metadata = os.path.join(self._pending_dir, name)
            try:
                with open(metadata, encoding='utf-8') as stream:
                    record = json.load(stream)
                snapshot = metadata[:-5]
                if (record.get('host') == host and record.get('remote') == remote
                        and os.path.isfile(snapshot)):
                    candidates.append((float(record['timestamp']), snapshot))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
        if candidates:
            snapshot = max(candidates)[1]
            self._latest[path] = snapshot
            try:
                with atomic_writer(path, binary=True) as dst, open(snapshot, 'rb') as src:
                    shutil.copyfileobj(src, dst)
            except OSError as exc:
                self._set_state(path, 'failed', str(exc))
                return
            for _, older in candidates:
                if older != snapshot:
                    self._unlink(older)
            self._set_state(path, 'failed', t('remote.sync_recovered'))

    def state(self, path):
        return self._states.get(path, ('synced', ''))

    def pending(self, path):
        return path in self._latest or path in self._capture_failed

    def _set_state(self, path, state, error=''):
        self._states[path] = (state, error)
        self.state_changed.emit(path, state, error)

    def save(self, path):
        if path not in self._targets:
            return
        snapshot = None
        try:
            os.makedirs(self._pending_dir, mode=0o700, exist_ok=True)
            fd, snapshot = tempfile.mkstemp(prefix='.stellar-sync-', dir=self._pending_dir)
            with os.fdopen(fd, 'wb') as dst, open(path, 'rb') as src:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            host, remote, _ = self._targets[path]
            with atomic_writer(snapshot + '.json') as metadata:
                json.dump({'host': host, 'remote': remote, 'timestamp': time.time()}, metadata)
        except OSError as exc:
            if snapshot:
                self._unlink(snapshot)
            self._capture_failed.add(path)
            self._set_state(path, 'failed', str(exc))
            return
        self._capture_failed.discard(path)
        previous = self._latest.get(path)
        self._latest[path] = snapshot
        if previous and previous != self._active.get(path):
            self._unlink(previous)
        self._set_state(path, 'pending')
        self.retry(path)

    def retry(self, path):
        if path in self._active or path not in self._targets:
            return
        if path in self._capture_failed or path not in self._latest:
            self.save(path)
            return
        host, remote, original = self._targets[path]
        snapshot = self._latest[path]
        try:
            session = self._session_for(host, original)
            if session is None or not session.is_connected():
                raise RuntimeError(t('remote.sync_disconnected'))
            self._active[path] = snapshot
            self._set_state(path, 'uploading')
            future = session.submit(session.upload, snapshot, remote)
        except Exception as exc:
            self._active.pop(path, None)
            self._set_state(path, 'failed', str(exc))
            return

        def done(f):
            try:
                f.result()
                error = ''
            except Exception as exc:
                error = str(exc)
            try:
                self._completed.emit(path, snapshot, error)
            except RuntimeError:  # Window was destroyed while the upload was running.
                pass  # Keep the durable snapshot for recovery after reopening.
        future.add_done_callback(done)

    def _finish(self, path, snapshot, error):
        self._active.pop(path, None)
        if path in self._capture_failed:
            # Keep the last durable version until the newer local save can be backed up.
            if self._latest.get(path) != snapshot:
                self._unlink(snapshot)
        elif self._latest.get(path) != snapshot:
            self._unlink(snapshot)
            self.retry(path)
        elif error:
            self._set_state(path, 'failed', error)
        else:
            self._latest.pop(path, None)
            self._unlink(snapshot)
            self._set_state(path, 'synced')
