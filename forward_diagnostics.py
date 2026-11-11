"""Manual, bounded TCP diagnostics. Never create forwards or authenticate again."""
from dataclasses import dataclass
import json
import shlex
import socket
import struct
import subprocess
import time

from i18n import t
import ssh_control


@dataclass(frozen=True)
class Check:
    status: str
    detail: str = ''


@dataclass(frozen=True)
class ForwardCheck:
    kind: str
    forward_state: str
    listen_host: str
    listen_port: int
    target_host: str
    target_port: int
    listener: Check
    target: Check


def parse_target(value: str) -> tuple:
    host, separator, port = str(value).strip().rpartition(':')
    if not separator or not host:
        raise ValueError(t('remote.fwd_probe_target_format'))
    ssh_control.forward_args(dict(type='L', bind_port=1, dest_host=host, dest_port=port))
    return host.strip(), int(port)


def _connect_host(host):
    return '127.0.0.1' if host == '0.0.0.0' else host


def _tcp(host, port, timeout):
    try:
        with socket.create_connection((_connect_host(host), port), timeout=timeout):
            return Check('ok')
    except OSError as exc:
        return Check('failed', str(exc))


_REMOTE_PREFIX = 'STELLAR_TCP_RESULT='
_REMOTE_PROGRAM = '''import json, socket, sys
try:
    sock = socket.create_connection((sys.argv[1], int(sys.argv[2])), float(sys.argv[3]))
    sock.close()
    result = {'status': 'ok', 'detail': ''}
except (OSError, IOError) as error:
    result = {'status': 'failed', 'detail': str(error)}
print('STELLAR_TCP_RESULT=' + json.dumps(result))
'''


def _remote_tcp(cfg, host, port, timeout):
    ctl = ssh_control.control_path_for(cfg)
    identity = ssh_control._forward_socket_identity(ctl) if ctl else None
    if identity is None or not ssh_control.master_pid(cfg):
        return Check('unknown', t('remote.fwd_master_gone'))
    arguments = ' '.join(shlex.quote(str(value)) for value in (
        '-c', _REMOTE_PROGRAM, _connect_host(host), port, timeout))
    missing = _REMOTE_PREFIX + json.dumps({'status': 'unknown', 'detail': 'missing_probe_tool'})
    command = (
        f'if command -v python3 >/dev/null 2>&1; then exec python3 {arguments}; '
        f'elif command -v python >/dev/null 2>&1; then exec python {arguments}; '
        f'else printf "%s\\n" {shlex.quote(missing)}; fi')
    try:
        proc = subprocess.run([
            'ssh', '-T', '-o', f'ControlPath={ctl}', '-o', 'ControlMaster=no',
            '-o', 'BatchMode=yes', '-o', 'ProxyCommand=false', '-o', 'ConnectTimeout=3',
            '-o', 'ConnectionAttempts=1', '-o', 'ClearAllForwardings=yes',
            '-o', 'RemoteCommand=none', '-o', 'LogLevel=ERROR',
            ssh_control.ssh_target(cfg), command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout + 5)
        if ssh_control._forward_socket_identity(ctl) != identity:
            return Check('unknown', t('remote.fwd_master_gone'))
        if proc.returncode != 0:
            detail = (proc.stderr or b'').decode('utf-8', 'replace').strip()[-500:]
            return Check('unknown', t('remote.fwd_probe_remote_unavailable', error=detail))
        for line in reversed((proc.stdout or b'').decode('utf-8', 'replace').splitlines()):
            if not line.startswith(_REMOTE_PREFIX):
                continue
            result = json.loads(line[len(_REMOTE_PREFIX):])
            if not isinstance(result, dict) or result.get('status') not in ('ok', 'failed', 'unknown'):
                break
            detail = str(result.get('detail') or '')[:500]
            if detail == 'missing_probe_tool':
                detail = t('remote.fwd_probe_missing_tool')
            return Check(result['status'], detail)
        return Check('unknown', t('remote.fwd_probe_remote_unavailable', error=''))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return Check('unknown', t('remote.fwd_probe_remote_unavailable', error=str(exc)[:500]))


def _socks(host, port, target_host, target_port, timeout):
    greeting_ok = False
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((_connect_host(host), port), timeout=timeout) as stream:
            def receive(count):
                data = b''
                while len(data) < count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(t('remote.fwd_probe_socks_timeout'))
                    stream.settimeout(remaining)
                    chunk = stream.recv(count - len(data))
                    if not chunk:
                        raise OSError(t('remote.fwd_probe_socks_eof'))
                    data += chunk
                return data
            stream.sendall(b'\x05\x01\x00')
            if receive(2) != b'\x05\x00':
                raise OSError(t('remote.fwd_probe_bad_socks'))
            greeting_ok = True
            address = target_host.encode('ascii')
            if len(address) > 255:
                raise ValueError(t('remote.fwd_probe_target_format'))
            stream.sendall(b'\x05\x01\x00\x03' + bytes([len(address)])
                           + address + struct.pack('!H', target_port))
            header = receive(4)
            if header[0] != 5 or header[2] != 0:
                raise OSError(t('remote.fwd_probe_bad_socks'))
            if header[1] != 0:
                return Check('ok'), Check('failed', t('remote.fwd_probe_socks_rejected', code=header[1]))
            length = {1: 4, 4: 16}.get(header[3])
            if header[3] == 3:
                length = receive(1)[0]
            if length is None:
                raise OSError(t('remote.fwd_probe_bad_socks'))
            receive(length + 2)
            return Check('ok'), Check('ok')
    except (OSError, ValueError) as exc:
        if greeting_ok:
            return Check('ok'), Check('failed', str(exc))
        return Check('failed', str(exc)), Check('unknown', t('remote.fwd_probe_no_socks_path'))


def check_forward(cfg, spec, target=None, timeout=3.0) -> ForwardCheck:
    """Check listener and destination independently; only SOCKS confirms a CONNECT path.

    L: listener TCP locally, destination TCP from the existing SSH master.
    R: listener TCP from the SSH host, destination TCP from this computer.
    D: SOCKS5 greeting and CONNECT to the explicitly supplied destination.
    No application protocol requests or credentials are sent to the destination.
    """
    ssh_control.forward_args(spec)
    kind = str(spec.get('type') or 'L').upper()
    host, port = str(spec.get('bind_host') or '127.0.0.1').strip(), int(spec['bind_port'])
    if kind == 'D':
        if target is None:
            raise ValueError(t('remote.fwd_probe_target_format'))
        target_host, target_port = parse_target(target)
    else:
        target_host = str(spec.get('dest_host') or '127.0.0.1').strip()
        target_port = int(spec['dest_port'])
    state = ssh_control.forward_statuses(cfg, [spec])[0]
    listener = Check('skipped', t('remote.fwd_probe_listener_unverified'))
    if kind == 'D':
        destination = Check('skipped', t('remote.fwd_probe_listener_unverified'))
        if state == 'running':
            listener, destination = _socks(host, port, target_host, target_port, timeout)
    elif kind == 'L':
        if state == 'running':
            listener = _tcp(host, port, timeout)
        destination = _remote_tcp(cfg, target_host, target_port, timeout)
    else:
        if state == 'running':
            listener = _remote_tcp(cfg, host, port, timeout)
        destination = _tcp(target_host, target_port, timeout)
    return ForwardCheck(kind, state, host, port, target_host, target_port, listener, destination)
