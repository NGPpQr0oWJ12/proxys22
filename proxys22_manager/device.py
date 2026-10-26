"""SSH management with host-key verification and guarded OpenWrt upgrades."""
import base64
from datetime import datetime, timezone
import getpass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sys
import tarfile
import time
import uuid
import zlib

from .releases import FirmwareImage, TARGET, verify_image


class DeviceError(RuntimeError):
    pass


def private_file(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
        output.write(body)
    return path


def validate_board(board):
    if not isinstance(board, dict) or board.get('board_name') != 'keenetic,kn-1112':
        raise DeviceError('Device model is not Keenetic KN-1112. Flashing refused.')
    if board.get('release', {}).get('target') != TARGET:
        raise DeviceError('Unexpected OpenWrt target. Flashing refused.')


def verify_backup(body, required):
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body), mode='rb') as compressed:
            unpacked = 0
            while block := compressed.read(65536):
                unpacked += len(block)
                if unpacked > 64 * 1024 * 1024:
                    raise DeviceError('Unpacked backup exceeds the size limit.')
        with tarfile.open(fileobj=io.BytesIO(body), mode='r:gz') as archive:
            names = {item.name.removeprefix('./').lstrip('/') for item in archive.getmembers() if item.isfile()}
            if 'etc/config/network' not in names or any(name.lstrip('/') not in names for name in required):
                raise DeviceError('Backup does not contain all existing network settings and VPN profiles.')
            for item in archive.getmembers():
                if item.isfile():
                    source = archive.extractfile(item)
                    if source is not None:
                        while source.read(65536):
                            pass
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise DeviceError('Router backup is not a valid readable archive.') from exc


class Router:
    def __init__(self, host, port=22, user='root', identity=None, state_dir=None):
        if not isinstance(host, str) or not host or any(c.isspace() for c in host) or host.startswith('-'):
            raise DeviceError('Specify a router hostname or IP address.')
        if not 1 <= port <= 65535:
            raise DeviceError('SSH port must be between 1 and 65535.')
        self.host, self.port, self.user, self.identity = host, port, user, identity
        self.state_dir = Path(state_dir or Path.home() / '.proxys22')
        self.client = None

    def __enter__(self):
        try:
            import paramiko
        except ImportError as exc:
            raise DeviceError('Install requirements-manager.txt or use the packaged executable.') from exc
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        known_hosts = self.state_dir / 'known_hosts'
        if not known_hosts.exists():
            private_file(known_hosts, b'')

        class ConfirmHost(paramiko.MissingHostKeyPolicy):
            def missing_host_key(policy, client, hostname, key):
                fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
                print(f'New SSH host: {hostname}\n{key.get_name()} SHA256:{fingerprint}')
                if input('Verify the fingerprint on the device. Type TRUST to save it: ').strip() != 'TRUST':
                    raise DeviceError('SSH host key was not accepted.')
                client.get_host_keys().add(hostname, key.get_name(), key)
                client.save_host_keys(str(known_hosts))

        def connect(password=None, passphrase=None):
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.load_host_keys(str(known_hosts))
            client.set_missing_host_key_policy(ConfirmHost())
            try:
                client.connect(self.host, port=self.port, username=self.user,
                               key_filename=str(self.identity) if self.identity else None,
                               password=password, passphrase=passphrase,
                               timeout=12, banner_timeout=12, auth_timeout=15,
                               allow_agent=True, look_for_keys=True)
                return client
            except Exception:
                client.close()
                raise
        try:
            try:
                self.client = connect()
            except paramiko.PasswordRequiredException:
                if not sys.stdin.isatty():
                    raise DeviceError('An encrypted SSH key needs an interactive terminal.')
                self.client = connect(passphrase=getpass.getpass('SSH key passphrase: '))
            except paramiko.SSHException as exc:
                if isinstance(exc, paramiko.AuthenticationException) or str(exc) == 'No authentication methods available':
                    if not sys.stdin.isatty():
                        raise DeviceError('Password authentication needs an interactive terminal; use --identity for automation.')
                    self.client = connect(password=getpass.getpass('Router SSH password: '))
                else:
                    raise
        except paramiko.BadHostKeyException as exc:
            raise DeviceError('SSH host key changed. Verify the device identity; connection refused.') from exc
        except (paramiko.SSHException, OSError) as exc:
            raise DeviceError('SSH connection failed. Check the address, Ethernet connection and credentials.') from exc
        return self

    def __exit__(self, *_args):
        if self.client:
            self.client.close()

    def run(self, command, data=None, timeout=30, limit=2 * 1024 * 1024):
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise DeviceError('SSH connection is closed.')
        channel = transport.open_session(timeout=12)
        output, errors = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            channel.settimeout(timeout)
            channel.exec_command(command)
            if data is not None:
                channel.sendall(data)
            channel.shutdown_write()
            while True:
                while channel.recv_ready():
                    output.extend(channel.recv(65536))
                    if len(output) > limit:
                        raise DeviceError('Device response exceeded the size limit.')
                while channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(65536))
                    if len(errors) > 65536:
                        raise DeviceError('Device error response exceeded the size limit.')
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    if channel.recv_exit_status() != 0:
                        raise DeviceError('Device command failed. No forced upgrade will be attempted.')
                    return bytes(output)
                if time.monotonic() >= deadline:
                    raise DeviceError('Device command timed out.')
                time.sleep(0.02)
        except (socket.timeout, EOFError, OSError) as exc:
            raise DeviceError('SSH command interrupted. Check device status before retrying.') from exc
        finally:
            channel.close()

    def board(self):
        try:
            board = json.loads(self.run('ubus call system board'))
            validate_board(board)
            return board
        except (ValueError, TypeError) as exc:
            raise DeviceError('Cannot identify the device through OpenWrt ubus.') from exc

    def firmware(self):
        try:
            release = json.loads(self.run('cat /etc/proxys22-release'))
        except ValueError as exc:
            raise DeviceError('The device does not report a valid proxys22 release.') from exc
        if not isinstance(release, dict) or release.get('name') != 'proxys22':
            raise DeviceError('Only an installed proxys22 system can be upgraded.')
        return release

    def backup(self, directory):
        self.board()
        required = self.run('for f in /etc/config/network /etc/config/wireless /etc/wifi-proxy/initialized '
                            '/etc/wifi-proxy/client.json /etc/wifi-proxy/awg.json; do '
                            'if [ -f "$f" ]; then printf "%s\\n" "$f"; fi; done').decode().splitlines()
        if '/etc/wifi-proxy/initialized' not in required:
            raise DeviceError('Initialization marker is missing; preserve the existing settings before upgrading.')
        body = self.run('umask 077; f=$(mktemp /tmp/proxys22-backup.XXXXXX) || exit 1; '
                        'sysupgrade -b "$f" >&2 && cat "$f"; result=$?; rm -f "$f"; exit "$result"',
                        timeout=90, limit=16 * 1024 * 1024)
        verify_backup(body, required)
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        path = Path(directory) / f'proxys22-backup-{timestamp}-{uuid.uuid4().hex[:8]}.tar.gz'
        private_file(path, body)
        private_file(path.with_suffix(path.suffix + '.sha256'),
                     f'{hashlib.sha256(body).hexdigest()}  {path.name}\n'.encode())
        return path

    def upgrade(self, image_path, image: FirmwareImage, backup_directory, confirm):
        if image.kind != 'sysupgrade':
            raise DeviceError('Upgrades require a sysupgrade image, never a factory image.')
        verify_image(image_path, image)
        self.board()
        self.firmware()
        backup = self.backup(backup_directory)
        print(f'Backup verified: {backup}')
        remote = '/tmp/proxys22-' + uuid.uuid4().hex + '-sysupgrade.bin'
        quoted = shlex.quote(remote)
        started = False
        try:
            self.run(f'umask 077; cat > {quoted}', Path(image_path).read_bytes(), timeout=180)
            digest = self.run(f'sha256sum {quoted}').decode('ascii', errors='replace').split()
            if not digest or digest[0] != image.sha256:
                raise DeviceError('On-device firmware SHA-256 verification failed.')
            self.run(f'sysupgrade -T {quoted}', timeout=120)
            if not confirm('All checks passed. Type UPDATE to flash and reboot, or press Enter to cancel: ', 'UPDATE'):
                raise DeviceError('Upgrade cancelled. Backup retained.')
            command = f'(sleep 2; sysupgrade {quoted}) </dev/null >/tmp/proxys22-upgrade.log 2>&1 & echo PROXYS22_STARTED'
            if self.run(command).strip() != b'PROXYS22_STARTED':
                raise DeviceError('Device did not confirm starting the upgrade. Check its status.')
            started = True
        finally:
            if not started:
                try:
                    self.run(f'rm -f {quoted}')
                except DeviceError:
                    pass
        return backup

    def diagnostics(self):
        board = self.board()
        release = self.firmware()
        snapshot = json.loads(self.run('ubus call wifi_proxy status'))
        tunnel, uplink = snapshot.get('tunnel', {}), snapshot.get('uplink', {})
        info = json.loads(self.run('ubus call system info'))
        # Deliberately select fields; never dump profiles, UCI, keys or raw logs.
        return {
            'model': board.get('model'), 'firmware': release.get('version'),
            'openwrt': board.get('release', {}).get('version'),
            'uptime': snapshot.get('system', {}).get('uptime'),
            'memory_total': info.get('memory', {}).get('total'),
            'memory_free': info.get('memory', {}).get('free'),
            'uplink_mode': uplink.get('mode'), 'uplink_connected': uplink.get('connected'),
            'protocol': tunnel.get('protocol'), 'configured': tunnel.get('configured'),
            'enabled': tunnel.get('enabled'), 'running': tunnel.get('running'),
            'route': tunnel.get('mode'), 'healthy': tunnel.get('healthy'),
            'reason': tunnel.get('reason'),
        }
