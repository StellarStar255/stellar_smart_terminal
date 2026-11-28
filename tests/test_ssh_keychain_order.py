from dataclasses import replace

import ssh_keychain
from ssh_session import HostConfig


def test_late_login_cannot_overwrite_newer_verified_password(monkeypatch):
    host = HostConfig("work", "work.invalid")
    saved = []
    monkeypatch.setattr(ssh_keychain, 'save', lambda host, password: saved.append(password))
    older = ssh_keychain.save_generation(host)
    newer = ssh_keychain.save_generation(host)
    assert ssh_keychain.save_if_current(host, newer, "new")
    assert not ssh_keychain.save_if_current(host, older, "old")
    assert saved == ["new"]


def test_rename_only_moves_credentials_for_matching_endpoint(monkeypatch):
    before = HostConfig("before", "server.invalid", user="alice", proxy_jump="bastion")
    after = replace(before, alias="after")
    moves = []
    monkeypatch.setattr(ssh_keychain, '_move_item', lambda a, b: moves.append((a, b)))
    for change in (dict(user="bob"), dict(port=2222), dict(hostname="elsewhere"), dict(proxy_jump=None)):
        ssh_keychain.rename(before, replace(after, **change))
    assert moves == []
    old_login = ssh_keychain.save_generation(before)
    ssh_keychain.rename(before, after)
    assert moves == [(before, after)]
    assert not ssh_keychain.save_if_current(before, old_login, "obsolete")
