"""Serialize editor uploads and publish worker results on the Qt thread."""
import os
import errno
import hashlib
import json
import math
import time
import weakref
from functools import partial
import tempfile
from concurrent.futures import ThreadPoolExecutor

from file_persistence import atomic_writer, file_sha256
from remote_edit import RemoteFileConflictError
from utils import get_data_dir
from app_logging import get_logger
from i18n import t

logger = get_logger(__name__)

from PyQt6.QtCore import QObject, QCoreApplication, pyqtSignal


class SnapshotIntegrityError(RuntimeError):
    def __init__(self):
        super().__init__(t('remote.sync_snapshot_corrupt'))


def _copy_and_hash(source, destination):
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(256 * 1024), b''):
        destination.write(chunk)
        digest.update(chunk)
    return digest.hexdigest()


class _RemoteEditorReference:
    """A pane's signals own this object; it never keeps the pane or sync alive."""
    def __init__(self):
        self.sync = None
        self.paths = set()

    def bind(self, sync, path):
        if self.sync is not None and self.sync() is not sync:
            self.release_all()
        self.sync = weakref.ref(sync)
        if path not in self.paths:
            self.paths.add(path)
            sync._references[path] = sync._references.get(path, 0) + 1
            sync._released.discard(path)

    def file_closed(self, path):
        if path not in self.paths:
            return
        self.paths.remove(path)
        sync = self.sync() if self.sync is not None else None
        if sync is not None:
            sync._release_reference(path)

    def release_all(self, *args):
        for path in list(self.paths):
            self.file_closed(path)
        self.sync = None


class _SnapshotLifecycle(QObject):
    """A separate Qt receiver stays alive while its owner emits destroyed."""
    def __init__(self, executor, sources, closing, pending_dir, preparations, recovery_executor):
        super().__init__(QCoreApplication.instance())
        self.executor, self.sources, self.closing = executor, sources, closing
        self.pending_dir = pending_dir
        self.preparations = preparations
        self.recovery_executor = recovery_executor

    def close(self):
        if self.closing[0]:
            return
        self.closing[0] = True
        for path, (_, data, host, remote, timestamp, baseline) in list(self.sources.items()):
            self.executor.submit(RemoteFileSync._write_snapshot,
                                 self.pending_dir, path, host, remote, data, timestamp, baseline)
        self.sources.clear()
        self.preparations.clear()
        self.recovery_executor.shutdown(wait=False, cancel_futures=True)
        self.executor.shutdown(wait=False)

    def owner_destroyed(self):
        self.close()
        self.deleteLater()


class RemoteFileSync(QObject):
    state_changed = pyqtSignal(str, str, str)
    released = pyqtSignal(str)
    release_requested = pyqtSignal(str)
    preparation_failed = pyqtSignal(str, str)
    _completed = pyqtSignal(str, str, str, bool)
    comparison_ready = pyqtSignal(str, object)
    _comparison_completed = pyqtSignal(str, object, object, str)
    _captured = pyqtSignal(str, int, str, str)
    _prepared_completed = pyqtSignal(str, object, object)

    def __init__(self, session_for, parent=None):
        super().__init__(parent)
        self._session_for = session_for
        self._conflicts = set()
        self._comparison_jobs = {}
        self._targets = {}
        self._preparations = {}
        self._prepared = {}
        self._references = {}
        self._released = set()
        self._baselines = {}
        self._snapshot_hashes = {}
        self._latest = {}
        self._active = {}
        self._states = {}
        self.release_requested.connect(self.unregister)
        self._completed.connect(self._finish)
        self._comparison_completed.connect(self._finish_comparison)
        self._captured.connect(self._capture_done)
        self._prepared_completed.connect(self._finish_preparation)
        self._capture_failed = set()
        self._pending_dir = os.path.join(get_data_dir(), 'remote_pending')
        self._generation = {}
        self._snapshot_timestamp = 0.0
        self._capture_sources = {}
        self._capture_jobs = {}
        self._capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='remote-snapshot')
        self._recovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='remote-recovery')
        self._closing = [False]
        # The cleanup receiver has an independent QObject lifetime.
        self._lifecycle = _SnapshotLifecycle(self._capture_executor, self._capture_sources,
                                            self._closing, self._pending_dir, self._preparations, self._recovery_executor)
        self.destroyed.connect(self._lifecycle.owner_destroyed)

    @staticmethod
    def _unlink(path):
        for candidate in (path, path + '.json'):
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning('Pending upload cleanup failed: %s', candidate, exc_info=True)

    def _discard_snapshot(self, path):
        self._snapshot_hashes.pop(path, None)
        self._unlink(path)

    def register(self, path, host, remote, session, baseline_hash=None, *, recover=True):
        self._targets[path] = (host, remote, session)
        self._states.setdefault(path, ('synced', ''))
        if self.pending(path):
            return
        if baseline_hash is not None:
            self._baselines[path] = baseline_hash
        if recover:
            self._apply_recovery(path, self._recover_file(self._pending_dir, path, host, remote))

    @staticmethod
    def _recover_file(pending_dir, path, host, remote):
        # Recover the newest unsent snapshot after a restart. Never auto-upload it.
        candidates = []
        try:
            names = os.listdir(pending_dir)
        except FileNotFoundError:
            return {}
        for name in names:
            if not name.endswith('.json'):
                continue
            metadata = os.path.join(pending_dir, name)
            try:
                with open(metadata, encoding='utf-8') as stream:
                    record = json.load(stream)
                snapshot = metadata[:-5]
                if (record.get('host') == host and record.get('remote') == remote
                        and os.path.isfile(snapshot)):
                    timestamp = float(record['timestamp'])
                    if math.isfinite(timestamp):
                        candidates.append((timestamp, snapshot, record))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
        candidates.sort(key=lambda item: item[:2], reverse=True)
        corrupt = False
        for index, (timestamp, snapshot, record) in enumerate(candidates):
            try:
                with atomic_writer(path, binary=True) as dst, open(snapshot, 'rb') as src:
                    digest = _copy_and_hash(src, dst)
                    expected = record.get('digest')
                    # Legacy records have neither a checksum nor a baseline.
                    # A missing/invalid checksum in a modern record is damage,
                    # not a reason to silently downgrade its integrity checks.
                    legacy = 'digest' not in record and 'baseline' not in record
                    if not legacy and expected != digest:
                        raise SnapshotIntegrityError()
            except SnapshotIntegrityError:
                corrupt = True
                logger.warning('Skipping damaged pending snapshot: %s', snapshot)
                continue
            except OSError as exc:
                return dict(snapshot=snapshot, baseline=record.get('baseline'),
                            digest=record.get('digest'), state='failed', error=str(exc))
            for _, older, _ in candidates[index + 1:]:
                RemoteFileSync._unlink(older)
            return dict(snapshot=snapshot, baseline=record.get('baseline'),
                        digest=digest, timestamp=timestamp, state='failed',
                        error=t('remote.sync_recovered_older' if corrupt else 'remote.sync_recovered'))
        if corrupt:
            # Keep damaged snapshots for inspection, and leave the cache intact.
            return dict(state='failed', error=t('remote.sync_snapshot_corrupt'))
        return {}

    def _apply_recovery(self, path, result):
        snapshot = result.get('snapshot')
        if snapshot:
            self._latest[path] = snapshot
            self._baselines[path] = result.get('baseline')
            self._snapshot_hashes[snapshot] = result.get('digest')
        self._snapshot_timestamp = max(self._snapshot_timestamp, result.get('timestamp', 0.0))
        if result.get('state'):
            self._set_state(path, result['state'], result.get('error', ''))

    def prepare_async(self, path, host, remote, session, callback, *, request_key=None):
        """Recover before opening, coalescing repeated requests for the same file."""
        if self._closing[0]:
            return
        self._released.discard(path)
        self.register(path, host, remote, session, recover=False)
        key = (host, remote)
        if self._prepared.get(path) == key or self.pending(path) or self._references.get(path):
            self._prepared[path] = key
            callback()
            return
        callback_key = object() if request_key is None else request_key
        existing = self._preparations.get(path)
        if existing and existing[1] == key:
            existing[2][callback_key] = callback
            return
        token = object()
        self._preparations[path] = (token, key, {callback_key: callback})
        future = self._recovery_executor.submit(self._recover_file, self._pending_dir, path, host, remote)
        def done(f):
            if self._closing[0] or f.cancelled():
                return
            try:
                result = f.result()
            except Exception as exc:
                logger.warning('Remote recovery failed: %s', path, exc_info=True)
                result = dict(state='failed', error=str(exc), blocked=True)
            try:
                self._prepared_completed.emit(path, token, result)
            except RuntimeError:
                logger.debug('Recovery completed after window closed')
        future.add_done_callback(done)

    def _finish_preparation(self, path, token, result):
        entry = self._preparations.get(path)
        if not entry or entry[0] is not token or self._closing[0]:
            return
        _, key, callbacks = self._preparations.pop(path)
        self._apply_recovery(path, result)
        if result.get('blocked'):
            self.preparation_failed.emit(path, result.get('error', ''))
            self.unregister(path)
            return
        self._prepared[path] = key
        if path in self._released and not self._references.get(path):
            self._forget_if_idle(path)
            return
        for callback in callbacks.values():
            try:
                callback()
            except (RuntimeError, ReferenceError):
                logger.debug('Remote open callback no longer available', exc_info=True)
                self.unregister(path)
            except Exception as exc:
                logger.warning('Remote open callback failed', exc_info=True)
                self.preparation_failed.emit(path, str(exc))
                self.unregister(path)

    def track_editor(self, pane, path):
        reference = getattr(pane, '_remote_file_reference', None)
        if reference is None:
            reference = _RemoteEditorReference()
            pane.remote_file_closed.connect(reference.file_closed)
            pane.destroyed.connect(reference.release_all)
            pane._remote_file_reference = reference
        reference.bind(self, path)

    def _release_reference(self, path):
        remaining = self._references.get(path, 0) - 1
        if remaining > 0:
            self._references[path] = remaining
        else:
            self._references.pop(path, None)
            self.unregister(path)

    def unregister(self, path):
        """Release synced state; defer removal while any edit or job is pending."""
        self._released.add(path)
        self._forget_if_idle(path)

    def _forget_if_idle(self, path):
        if (self._closing[0] or path not in self._released or self._references.get(path)
                or self.pending(path) or path in self._active or path in self._preparations
                or path in self._comparison_jobs):
            return
        for mapping in (self._targets, self._states, self._baselines, self._generation, self._prepared):
            mapping.pop(path, None)
        self._conflicts.discard(path)
        self._released.discard(path)
        self.released.emit(path)

    def state(self, path):
        return self._states.get(path, ('synced', ''))

    def pending(self, path):
        return (path in self._latest or path in self._capture_failed
                or path in self._capture_jobs or path in self._capture_sources)

    def _set_state(self, path, state, error=''):
        self._states[path] = (state, error)
        self.state_changed.emit(path, state, error)

    @staticmethod
    def _write_snapshot(pending_dir, path, host, remote, data, timestamp, baseline=None):
        snapshot = None
        try:
            os.makedirs(pending_dir, mode=0o700, exist_ok=True)
            fd, snapshot = tempfile.mkstemp(prefix='.stellar-sync-', dir=pending_dir)
            with os.fdopen(fd, 'wb') as dst:
                if data is not None:
                    dst.write(data)
                    digest = hashlib.sha256(data).hexdigest()
                else:
                    with open(path, 'rb') as src:
                        digest = _copy_and_hash(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            with atomic_writer(snapshot + '.json') as metadata:
                json.dump({'host': host, 'remote': remote, 'timestamp': timestamp,
                           'baseline': baseline, 'digest': digest}, metadata)
            return snapshot
        except Exception:
            if snapshot:
                RemoteFileSync._unlink(snapshot)
            raise

    def shutdown(self):
        self._comparison_jobs.clear()
        self._lifecycle.close()

    def save(self, path, data=None):
        """Queue immutable saved bytes; coalesce later saves while a copy is running."""
        if path not in self._targets or self._closing[0]:
            return
        host, remote, _ = self._targets[path]
        generation = self._generation.get(path, 0) + 1
        self._generation[path] = generation
        self._snapshot_timestamp = max(time.time(), math.nextafter(self._snapshot_timestamp, math.inf))
        self._capture_sources[path] = (generation, data, host, remote, self._snapshot_timestamp, self._baselines.get(path))
        self._capture_failed.discard(path)
        self._set_state(path, 'pending')
        self._start_capture(path)

    def _start_capture(self, path):
        if path in self._capture_jobs or path not in self._capture_sources or self._closing[0]:
            return
        generation, data, host, remote, timestamp, baseline = self._capture_sources.pop(path)
        self._capture_jobs[path] = generation
        future = self._capture_executor.submit(self._write_snapshot, self._pending_dir,
                                               path, host, remote, data, timestamp, baseline)
        def done(f):
            try:
                snapshot, error = f.result(), ''
            except Exception as exc:
                snapshot, error = '', str(exc)
            try:
                self._captured.emit(path, generation, snapshot, error)
            except RuntimeError:  # Closed window: the durable snapshot remains recoverable.
                logger.debug('Snapshot completed after window closed')
        future.add_done_callback(done)

    def _capture_done(self, path, generation, snapshot, error):
        self._capture_jobs.pop(path, None)
        if self._closing[0]:
            return  # Keep durable content for recovery; do not start an upload.
        if generation != self._generation[path]:
            if snapshot:
                self._discard_snapshot(snapshot)
            self._start_capture(path)
            return
        if error:
            self._capture_failed.add(path)
            self._set_state(path, 'failed', error)
            return
        try:
            with open(snapshot + '.json', encoding='utf-8') as stream:
                self._snapshot_hashes[snapshot] = json.load(stream)['digest']
        except (OSError, ValueError, KeyError) as exc:
            self._capture_failed.add(path)
            self._set_state(path, 'failed', str(exc))
            return
        previous = self._latest.get(path)
        self._latest[path] = snapshot
        if previous and previous != self._active.get(path):
            self._discard_snapshot(previous)
        self.retry(path)

    def retry(self, path):
        if path in self._conflicts:
            self._set_state(path, 'conflict', t('remote.sync_conflict'))
            return
        if (path in self._active or path not in self._targets or self._closing[0]
                or path in self._capture_jobs or path in self._capture_sources):
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
            future = session.submit(partial(self._upload_checked, session, self._baselines.get(path)),
                                    snapshot, remote)
        except Exception as exc:
            self._active.pop(path, None)
            self._set_state(path, 'failed', str(exc))
            return

        def done(f):
            try:
                f.result()
                error, conflict = '', False
            except Exception as exc:
                error, conflict = str(exc), isinstance(exc, RemoteFileConflictError)
            try:
                self._completed.emit(path, snapshot, error, conflict)
            except RuntimeError:  # Window was destroyed while the upload was running.
                pass  # Keep the durable snapshot for recovery after reopening.
        future.add_done_callback(done)

    def compare(self, path):
        if (path not in self._conflicts or self._closing[0]
                or path in self._comparison_jobs):
            return
        host, remote, original = self._targets[path]
        token = object()
        self._comparison_jobs[path] = token
        try:
            session = self._session_for(host, original)
            if session is None or not session.is_connected():
                raise RuntimeError(t('remote.sync_disconnected'))
            future = session.submit(self._read_for_comparison, session, remote)
        except Exception as exc:
            self._comparison_jobs.pop(path, None)
            self._set_state(path, 'conflict', str(exc))
            return
        def done(f):
            try:
                data, error = f.result(), ''
            except Exception as exc:
                data, error = None, str(exc)
            try:
                self._comparison_completed.emit(path, token, data, error)
            except RuntimeError:
                logger.debug('Remote comparison finished after window closed')
        future.add_done_callback(done)

    @staticmethod
    def _read_for_comparison(session, remote):
        try:
            return session.read_file(remote, max_bytes=20 * 1024 * 1024)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None  # Distinct from an existing empty file.
            raise

    def _finish_comparison(self, path, token, data, error):
        if self._comparison_jobs.get(path) is not token:
            return
        self._comparison_jobs.pop(path)
        if self._closing[0] or path not in self._conflicts:
            return
        if error:
            self._set_state(path, 'conflict', error)
        else:
            self.comparison_ready.emit(path, data)
        self._forget_if_idle(path)

    def resolve(self, path, baseline_hash):
        if path in self._conflicts:
            self._comparison_jobs.pop(path, None)
            self._baselines[path] = baseline_hash
            self._conflicts.discard(path)

    @staticmethod
    def _upload_checked(session, baseline, snapshot, remote):
        if baseline is None:
            # Old pending records have no reliable baseline: never overwrite blindly.
            raise RemoteFileConflictError()
        with open(snapshot + '.json', encoding='utf-8') as stream:
            record = json.load(stream)
        if record.get('digest') != file_sha256(snapshot):
            raise SnapshotIntegrityError()
        record['baseline'] = baseline
        with atomic_writer(snapshot + '.json') as stream:
            json.dump(record, stream)
        session.upload_for_edit(snapshot, remote, baseline)

    def _finish(self, path, snapshot, error, conflict=False):
        self._active.pop(path, None)
        if conflict:
            self._conflicts.add(path)
        if not error:
            self._baselines[path] = self._snapshot_hashes.get(snapshot)
        if (path in self._capture_failed or path in self._capture_jobs
                or path in self._capture_sources):
            # Keep the last durable version until the newer local save can be backed up.
            if self._latest.get(path) != snapshot:
                self._discard_snapshot(snapshot)
        elif self._latest.get(path) != snapshot:
            self._discard_snapshot(snapshot)
            self.retry(path)
        elif error:
            self._set_state(path, 'conflict' if conflict else 'failed', error)
        else:
            self._latest.pop(path, None)
            self._discard_snapshot(snapshot)
            self._set_state(path, 'synced')
        if conflict:
            self._set_state(path, 'conflict', error)
        retained = set(self._latest.values()) | set(self._active.values())
        self._snapshot_hashes = {p: h for p, h in self._snapshot_hashes.items() if p in retained}
        self._forget_if_idle(path)
