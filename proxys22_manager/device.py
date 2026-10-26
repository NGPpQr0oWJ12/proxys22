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

from .releases import FirmwareImage, PROFILE, TARGET, verify_image


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
        raise DeviceError('Модель устройства не соответствует Keenetic KN-1112. Запись прошивки запрещена.')
    release = board.get('release')
    if not isinstance(release, dict) or release.get('target') != TARGET:
        raise DeviceError('Платформа OpenWrt не соответствует ожидаемой. Запись прошивки запрещена.')


def identify_board(board):
    if not isinstance(board, dict) or not isinstance(board.get('board_name'), str) or not board['board_name'].strip():
        raise DeviceError('Устройство не сообщает идентификатор платы. Автоопределение модели невозможно; запись запрещена.')

    def label(value):
        return ''.join(character for character in value if character.isprintable())[:160] if isinstance(value, str) else ''

    release = board.get('release') if isinstance(board.get('release'), dict) else {}
    identity = {'model': label(board.get('model')) or label(board['board_name']),
                'board_name': label(board['board_name']), 'target': label(release.get('target')),
                'cpu': label(board.get('system')), 'openwrt': label(release.get('version')),
                'supported': False, 'profile': None}
    try:
        validate_board(board)
    except DeviceError as exc:
        identity['reason'] = str(exc)
    else:
        identity.update(model='Keenetic Start KN-1112', supported=True, profile=PROFILE, reason='Совместимая сборка доступна.')
    return identity


def verify_backup(body, required):
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body), mode='rb') as compressed:
            unpacked = 0
            while block := compressed.read(65536):
                unpacked += len(block)
                if unpacked > 64 * 1024 * 1024:
                    raise DeviceError('Распакованная резервная копия превышает допустимый размер.')
        with tarfile.open(fileobj=io.BytesIO(body), mode='r:gz') as archive:
            names = {item.name.removeprefix('./').lstrip('/') for item in archive.getmembers() if item.isfile()}
            if 'etc/config/network' not in names or any(name.lstrip('/') not in names for name in required):
                raise DeviceError('В резервной копии отсутствует часть действующих настроек сети или профилей VPN.')
            for item in archive.getmembers():
                if item.isfile():
                    source = archive.extractfile(item)
                    if source is not None:
                        while source.read(65536):
                            pass
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise DeviceError('Не удалось прочитать резервную копию роутера: архив повреждён или имеет неверный формат.') from exc


class Router:
    def __init__(self, host, port=22, user='root', identity=None, state_dir=None, *, on_stage=None, progress=None):
        if not isinstance(host, str) or not host or any(c.isspace() for c in host) or host.startswith('-'):
            raise DeviceError('Укажите имя узла или IP-адрес роутера.')
        if not 1 <= port <= 65535:
            raise DeviceError('Порт SSH должен быть числом от 1 до 65535.')
        self.host, self.port, self.user, self.identity = host, port, user, identity
        self.state_dir = Path(state_dir or Path.home() / '.proxys22')
        self.client = None
        self.on_stage, self.progress = on_stage, progress

    def __enter__(self):
        try:
            import paramiko
        except ImportError as exc:
            raise DeviceError('Установите зависимости из requirements-manager.txt или используйте готовую программу.') from exc
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        known_hosts = self.state_dir / 'known_hosts'
        if not known_hosts.exists():
            private_file(known_hosts, b'')

        class ConfirmHost(paramiko.MissingHostKeyPolicy):
            def missing_host_key(policy, client, hostname, key):
                fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
                print(f'Новое SSH-устройство: {hostname}\n{key.get_name()} SHA256:{fingerprint}', file=sys.stderr)
                print('Сверьте отпечаток ключа с устройством. Введите ДОВЕРЯЮ, чтобы сохранить ключ\n'
                      'и разрешить это подключение. Это не запускает запись прошивки.\n  > ',
                      end='', file=sys.stderr, flush=True)
                if input().strip().casefold() != 'доверяю':
                    raise DeviceError('Ключ SSH устройства не принят. Подключение отменено.')
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
                    raise DeviceError('Для разблокировки зашифрованного SSH-ключа нужен интерактивный терминал.')
                self.client = connect(passphrase=getpass.getpass('Пароль SSH-ключа: '))
            except paramiko.SSHException as exc:
                if isinstance(exc, paramiko.AuthenticationException) or str(exc) == 'No authentication methods available':
                    if not sys.stdin.isatty():
                        raise DeviceError('Для ввода пароля нужен интерактивный терминал. Для автоматизации используйте --identity.')
                    self.client = connect(password=getpass.getpass('Пароль SSH роутера: '))
                else:
                    raise
        except paramiko.BadHostKeyException as exc:
            raise DeviceError('Ключ SSH устройства изменился. Проверьте подлинность роутера. Подключение запрещено.') from exc
        except (paramiko.SSHException, OSError) as exc:
            raise DeviceError('Не удалось подключиться по SSH. Проверьте адрес, Ethernet-соединение и данные для входа.') from exc
        return self

    def __exit__(self, *_args):
        if self.client:
            self.client.close()

    def run(self, command, data=None, timeout=30, limit=2 * 1024 * 1024, progress=None):
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise DeviceError('SSH-соединение закрыто.')
        channel = transport.open_session(timeout=12)
        output, errors = bytearray(), bytearray()
        deadline = time.monotonic() + timeout
        try:
            channel.settimeout(timeout)
            channel.exec_command(command)
            if data is not None:
                total = len(data)
                if progress:
                    progress(0, total)
                for offset in range(0, total, 65536):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DeviceError('Превышено время ожидания команды на устройстве.')
                    channel.settimeout(remaining)
                    block = data[offset:offset + 65536]
                    channel.sendall(block)
                    if progress:
                        progress(offset + len(block), total)
            channel.shutdown_write()
            while True:
                while channel.recv_ready():
                    output.extend(channel.recv(65536))
                    if len(output) > limit:
                        raise DeviceError('Ответ устройства превышает допустимый размер.')
                while channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(65536))
                    if len(errors) > 65536:
                        raise DeviceError('Сообщение об ошибке устройства превышает допустимый размер.')
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    if channel.recv_exit_status() != 0:
                        raise DeviceError('Команда на устройстве завершилась с ошибкой. Принудительная запись прошивки не выполняется.')
                    return bytes(output)
                if time.monotonic() >= deadline:
                    raise DeviceError('Превышено время ожидания команды на устройстве.')
                time.sleep(0.02)
        except (socket.timeout, EOFError, OSError) as exc:
            raise DeviceError('Выполнение команды SSH прервано. Перед повторной попыткой проверьте состояние устройства.') from exc
        finally:
            channel.close()

    def _read_board(self):
        try:
            board = json.loads(self.run('ubus call system board'))
        except (ValueError, TypeError) as exc:
            raise DeviceError('Не удалось определить модель устройства через OpenWrt ubus.') from exc
        if not isinstance(board, dict):
            raise DeviceError('Устройство не сообщает данные о модели через OpenWrt ubus.')
        return board

    def identify(self):
        return identify_board(self._read_board())

    def board(self):
        board = self._read_board()
        validate_board(board)
        return board

    def firmware(self):
        try:
            release = json.loads(self.run('cat /etc/proxys22-release'))
        except ValueError as exc:
            raise DeviceError('Устройство не сообщает корректные данные о версии proxys22.') from exc
        if not isinstance(release, dict) or release.get('name') != 'proxys22':
            raise DeviceError('Обновление доступно только для устройства с уже установленной proxys22.')
        return release

    def backup(self, directory):
        self.board()
        required = self.run('for f in /etc/config/network /etc/config/wireless /etc/wifi-proxy/initialized '
                            '/etc/wifi-proxy/client.json /etc/wifi-proxy/awg.json; do '
                            'if [ -f "$f" ]; then printf "%s\\n" "$f"; fi; done').decode().splitlines()
        if '/etc/wifi-proxy/initialized' not in required:
            raise DeviceError('Отсутствует маркер инициализации. Перед обновлением обеспечьте сохранение действующих настроек.')
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
        stage = getattr(self, 'on_stage', None) or (lambda message: None)
        stage('Проверка образа прошивки и модели роутера')
        if image.kind != 'sysupgrade':
            raise DeviceError('Для обновления нужен образ sysupgrade. Образ factory использовать нельзя.')
        verify_image(image_path, image)
        self.board()
        self.firmware()
        stage('Создание и проверка резервной копии настроек')
        backup = self.backup(backup_directory)
        print(f'Резервная копия проверена: {backup}')
        remote = '/tmp/proxys22-' + uuid.uuid4().hex + '-sysupgrade.bin'
        quoted = shlex.quote(remote)
        started = False
        try:
            stage('Передача прошивки на роутер')
            upload_options = {'timeout': 180}
            if getattr(self, 'progress', None):
                upload_options['progress'] = self.progress
            self.run(f'umask 077; cat > {quoted}', Path(image_path).read_bytes(), **upload_options)
            stage('Проверка SHA-256 прошивки на роутере')
            digest = self.run(f'sha256sum {quoted}').decode('ascii', errors='replace').split()
            if not digest or digest[0] != image.sha256:
                raise DeviceError('Контрольная сумма SHA-256 файла на роутере не совпадает с проверенным образом.')
            stage('Проверка совместимости образа: sysupgrade -T')
            self.run(f'sysupgrade -T {quoted}', timeout=120)
            stage('Подтверждение записи прошивки владельцем')
            if not confirm('Все проверки пройдены. Введите ОБНОВИТЬ, чтобы записать прошивку и перезагрузить роутер,\n'
                           'или нажмите Enter для отмены: ', 'ОБНОВИТЬ'):
                raise DeviceError('Обновление отменено. Резервная копия сохранена.')
            stage('Запуск обновления и перезагрузки роутера')
            command = f'(sleep 2; sysupgrade {quoted}) </dev/null >/tmp/proxys22-upgrade.log 2>&1 & echo PROXYS22_STARTED'
            if self.run(command).strip() != b'PROXYS22_STARTED':
                raise DeviceError('Устройство не подтвердило запуск обновления. Проверьте его состояние.')
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
