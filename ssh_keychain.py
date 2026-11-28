"""Application-owned generic passwords in the macOS login Keychain."""
from itertools import count
import json
import sys
import threading

available = sys.platform == "darwin"
_SERVICE = "Stellar Smart Terminal SSH"
_write_lock = threading.Lock()
_sequence = count(1)
_last_change = {}


def save_generation(host):
    with _write_lock:
        return next(_sequence)


def save_if_current(host, generation, password):
    with _write_lock:
        if generation <= _last_change.get(host.credential_key, 0):
            return False
        save(host, password)
        _last_change[host.credential_key] = generation
        return True


def _query(host):
    import Security as sec
    return {
        sec.kSecClass: sec.kSecClassGenericPassword,
        sec.kSecAttrService: _SERVICE,
        sec.kSecAttrAccount: json.dumps(host.credential_key, ensure_ascii=False),
    }


def _check(status):
    if status != 0:
        raise OSError(status, "macOS Keychain operation failed")


def read(host):
    import Security as sec
    query = _query(host)
    query[sec.kSecReturnData] = True
    status, data = sec.SecItemCopyMatching(query, None)
    if status == sec.errSecItemNotFound:
        return None
    _check(status)
    try:
        return bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        # Another Keychain editor may have stored binary data in this item.
        raise OSError("Saved SSH password is not valid UTF-8") from None


def save(host, password):
    import Security as sec
    query = _query(host)
    attrs = {sec.kSecValueData: password.encode("utf-8")}
    status = sec.SecItemUpdate(query, attrs)
    if status == sec.errSecItemNotFound:
        status, _ = sec.SecItemAdd({**query, **attrs}, None)
        if status == sec.errSecDuplicateItem:
            status = sec.SecItemUpdate(query, attrs)
    _check(status)


def forget(host):
    # Serialize deletion with writes from other windows and invalidate earlier consent.
    with _write_lock:
        _delete(host)
        _last_change[host.credential_key] = next(_sequence)


def _delete(host):
    import Security as sec
    status = sec.SecItemDelete(_query(host))
    if status != sec.errSecItemNotFound:
        _check(status)


def rename(before, after):
    old_key, new_key = before.credential_key, after.credential_key
    if old_key == new_key or old_key[1:] != new_key[1:]:
        return
    with _write_lock:
        moved = _move_item(before, after)
        generation = next(_sequence)
        _last_change[old_key] = generation
        if moved:
            _last_change[new_key] = generation


def _move_item(before, after):
    import Security as sec
    account = sec.kSecAttrAccount
    status = sec.SecItemUpdate(_query(before), {account: _query(after)[account]})
    if status == sec.errSecItemNotFound:
        return False
    _check(status)
    return True
