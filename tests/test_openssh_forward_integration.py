"""Real OpenSSH, isolated keys/config, loopback-only servers; opt in explicitly.

STELLAR_SSH_INTEGRATION=1 python -m pytest tests/test_openssh_forward_integration.py -v
No user SSH configuration, authorized_keys, or existing masters are modified.
"""
import getpass
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time

import pytest

import ssh_control
import forward_diagnostics
from ssh_session import HostConfig

pytestmark = pytest.mark.skipif(
    os.environ.get('STELLAR_SSH_INTEGRATION') != '1' or os.name == 'nt',
    reason='Set STELLAR_SSH_INTEGRATION=1 on a POSIX host with ssh/sshd/ssh-keygen')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def receive(sock, count):
    result = b''
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        assert chunk, 'unexpected EOF'
        result += chunk
    return result


def socks_connect(port, target_port):
    sock = socket.create_connection(('127.0.0.1', port), timeout=3)
    try:
        sock.sendall(b'\x05\x01\x00')
        assert receive(sock, 2) == b'\x05\x00'
        sock.sendall(b'\x05\x01\x00\x01' + socket.inet_aton('127.0.0.1')
                     + struct.pack('!H', target_port))
        header = receive(sock, 4)
        assert header[:3] == b'\x05\x00\x00', header
        size = {1: 4, 4: 16}.get(header[3])
        if header[3] == 3:
            size = receive(sock, 1)[0]
        assert size is not None
        receive(sock, size + 2)
        return sock
    except BaseException:
        sock.close()
        raise


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            data = self.request.recv(128)
            if data:
                self.request.sendall(data)
                self.request.shutdown(socket.SHUT_WR)
        except (OSError, TimeoutError):
            # Status probes can connect without sending application data.
            return


class EchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalSSH:
    def __init__(self, directory, ssh, port, target_port):
        self.directory = directory
        self.ssh = ssh
        self.target_port = target_port
        self.cfg = HostConfig(alias='isolated-forward-test', hostname='127.0.0.1',
                              user=getpass.getuser(), port=port)
        self.master = None
        self.master_log = None

    def start(self):
        self.master_log = open(self.directory / 'master.log', 'ab')
        self.master = subprocess.Popen([
            self.ssh, '-F', os.devnull, '-M', '-N', '-S', ssh_control.control_path_for(self.cfg),
            '-p', str(self.cfg.port), '-i', str(self.directory / 'client_key'),
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ControlPersist=no',
            '-o', 'StrictHostKeyChecking=no', '-o', f'UserKnownHostsFile={self.directory / "known_hosts"}',
            '-o', 'ConnectTimeout=3', ssh_control.ssh_target(self.cfg)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.master_log)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if ssh_control.master_pid(self.cfg):
                return
            if self.master.poll() is not None:
                break
            time.sleep(0.03)
        raise AssertionError((self.directory / 'master.log').read_text())

    def stop(self):
        if self.master is not None:
            self.master.terminate()  # This Popen is exclusively owned by the test.
            try:
                self.master.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.master.kill()
                self.master.wait(timeout=3)
            self.master = None
        if self.master_log is not None:
            self.master_log.close()
            self.master_log = None

    def rule(self, kind='L'):
        return dict(type=kind, bind_host='127.0.0.1', bind_port=str(free_port()),
                    dest_host='127.0.0.1', dest_port=str(self.target_port))


@pytest.fixture(scope='module')
def local_ssh():
    ssh = shutil.which('ssh')
    sshd = shutil.which('sshd') or ('/usr/sbin/sshd' if Path('/usr/sbin/sshd').exists() else None)
    keygen = shutil.which('ssh-keygen')
    assert ssh and sshd and keygen, 'Integration mode requires ssh, sshd, ssh-keygen'
    with tempfile.TemporaryDirectory(prefix='st-ssh-', dir='/tmp') as temporary, \
         pytest.MonkeyPatch.context() as patcher, EchoServer(('127.0.0.1', 0), Echo) as echo:
        directory = Path(temporary)
        thread = threading.Thread(target=echo.serve_forever, daemon=True)
        thread.start()
        for key in ('host_key', 'client_key'):
            subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(directory / key)],
                           check=True, capture_output=True, timeout=10)
        port = free_port()
        configuration = directory / 'sshd_config'
        configuration.write_text('\n'.join([
            f'Port {port}', 'ListenAddress 127.0.0.1', f'HostKey {directory / "host_key"}',
            f'AuthorizedKeysFile {directory / "client_key.pub"}', f'PidFile {directory / "sshd.pid"}',
            'StrictModes no', 'PasswordAuthentication no', 'KbdInteractiveAuthentication no',
            'UsePAM no', 'PubkeyAuthentication yes', 'AllowTcpForwarding yes',
            'PermitRootLogin prohibit-password', 'LogLevel VERBOSE',
        ]) + '\n')
        # All SSH calls remain real, but are insulated from ~/.ssh/config.
        bindir = directory / 'bin'
        bindir.mkdir()
        wrapper = bindir / 'ssh'
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(ssh) + ' -F /dev/null "$@"\n')
        wrapper.chmod(0o700)
        patcher.setenv('PATH', str(bindir) + os.pathsep + os.environ.get('PATH', ''))
        patcher.setattr(ssh_control, '_ctl_dir', lambda: str(directory))
        with open(directory / 'sshd.log', 'wb') as log:
            daemon = subprocess.Popen([sshd, '-D', '-e', '-f', str(configuration)],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            harness = LocalSSH(directory, ssh, port, echo.server_address[1])
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if daemon.poll() is not None:
                        pytest.fail((directory / 'sshd.log').read_text())
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.03)
                harness.start()
                yield harness
            finally:
                harness.stop()
                daemon.terminate()
                try:
                    daemon.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=3)
                echo.shutdown()
                thread.join(timeout=3)


@pytest.mark.parametrize('kind', ['L', 'R', 'D'])
def test_real_forward_traffic_and_idempotent_stop(local_ssh, kind):
    h = local_ssh
    spec = h.rule(kind)
    ssh_control.forward_apply(h.cfg, spec)
    try:
        assert ssh_control.forward_statuses(h.cfg, [spec]) == ['running']
        connect = (socks_connect(int(spec['bind_port']), h.target_port) if kind == 'D' else
                   socket.create_connection(('127.0.0.1', int(spec['bind_port'])), timeout=3))
        with connect as stream:
            stream.sendall(b'real forwarded payload')
            assert receive(stream, 22) == b'real forwarded payload'
            assert stream.recv(1) == b''
    finally:
        ssh_control.forward_apply(h.cfg, spec, cancel=True)
    ssh_control.forward_apply(h.cfg, spec, cancel=True)
    assert ssh_control.forward_statuses(h.cfg, [spec]) == ['stopped']


def test_real_immediate_restart_after_traffic(local_ssh):
    h = local_ssh
    spec = h.rule()
    for _ in range(8):
        ssh_control.forward_apply(h.cfg, spec)
        try:
            with socket.create_connection(('127.0.0.1', int(spec['bind_port'])), timeout=3) as stream:
                stream.sendall(b'ping')
                assert receive(stream, 4) == b'ping'
                assert stream.recv(1) == b''
        finally:
            ssh_control.forward_apply(h.cfg, spec, cancel=True)


def test_fresh_application_process_recovers_and_stops_existing_rule(local_ssh):
    h = local_ssh
    spec = h.rule()
    ssh_control.forward_apply(h.cfg, spec)
    program = '''import json, sys, ssh_control
from ssh_session import HostConfig
directory, config, spec = sys.argv[1:]
ssh_control._ctl_dir = lambda: directory
host = HostConfig(**json.loads(config))
rule = json.loads(spec)
assert ssh_control.forward_statuses(host, [rule]) == ['running']
ssh_control.forward_apply(host, rule, cancel=True)
assert ssh_control.forward_statuses(host, [rule]) == ['stopped']
'''
    try:
        subprocess.run([sys.executable, '-c', program, str(h.directory), json.dumps(dict(
            alias=h.cfg.alias, hostname=h.cfg.hostname, user=h.cfg.user, port=h.cfg.port)),
            json.dumps(spec)], check=True, capture_output=True, timeout=10)
        assert ssh_control.forward_statuses(h.cfg, [spec]) == ['stopped']
    finally:
        ssh_control.forward_apply(h.cfg, spec, cancel=True)


def test_foreign_listener_is_preserved(local_ssh):
    h = local_ssh
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        spec = h.rule()
        spec['bind_port'] = str(listener.getsockname()[1])
        before = ssh_control.master_pid(h.cfg)
        with pytest.raises(ssh_control.LocalPortBusy):
            ssh_control.forward_force_apply(h.cfg, spec)
        assert ssh_control.master_pid(h.cfg) == before
        with socket.create_connection(listener.getsockname(), timeout=2):
            connection, _ = listener.accept()
            connection.close()


def test_master_loss_and_replacement_do_not_reuse_stale_status(local_ssh):
    h = local_ssh
    spec = h.rule('R')
    ssh_control.forward_apply(h.cfg, spec)
    h.stop()
    try:
        assert ssh_control.forward_statuses(h.cfg, [spec]) != ['running']
        with pytest.raises(ssh_control.MasterNotRunning):
            ssh_control.forward_apply(h.cfg, spec)
    finally:
        h.start()
    assert ssh_control.forward_statuses(h.cfg, [spec]) == ['unknown']
    ssh_control.forward_apply(h.cfg, spec)
    ssh_control.forward_apply(h.cfg, spec, cancel=True)


@pytest.mark.parametrize('kind', ['L', 'R', 'D'])
@pytest.mark.parametrize('reachable', [True, False])
def test_diagnostics_distinguish_running_forward_from_target_availability(local_ssh, kind, reachable):
    h = local_ssh
    spec = h.rule(kind)
    destination = h.target_port if reachable else free_port()
    spec['dest_port'] = str(destination)
    ssh_control.forward_apply(h.cfg, spec)
    before = ssh_control.master_pid(h.cfg)
    try:
        result = forward_diagnostics.check_forward(
            h.cfg, spec, f'127.0.0.1:{destination}' if kind == 'D' else None)
        assert result.forward_state == 'running'
        assert result.listener.status == 'ok', result
        assert result.target.status == ('ok' if reachable else 'failed'), result
        assert ssh_control.forward_statuses(h.cfg, [spec]) == ['running']
        assert ssh_control.master_pid(h.cfg) == before
    finally:
        ssh_control.forward_apply(h.cfg, spec, cancel=True)


def test_diagnostics_do_not_start_stopped_forward_or_reconnect(local_ssh):
    h = local_ssh
    spec = h.rule()
    result = forward_diagnostics.check_forward(h.cfg, spec)
    assert result.forward_state == 'stopped'
    assert result.listener.status == 'skipped'
    assert result.target.status == 'ok'
    assert not ssh_control.local_port_busy('127.0.0.1', spec['bind_port'])
    h.stop()
    try:
        result = forward_diagnostics.check_forward(h.cfg, spec)
        assert result.target.status == 'unknown'
        assert not ssh_control.master_pid(h.cfg)
    finally:
        h.start()
