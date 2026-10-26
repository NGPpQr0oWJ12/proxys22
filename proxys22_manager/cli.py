"""Interactive terminal menu and automation-friendly subcommands."""
import argparse
import importlib.util
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import sys

from . import VERSION
from .device import DeviceError, Router
from .releases import (Catalog, DEFAULT_REPO, MANIFEST_NAME, ReleaseError,
                       parse_manifest, verify_image)


def default_state():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'proxys22'
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'proxys22'


def confirm(prompt, expected):
    return input(prompt).strip() == expected


def load_recovery():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    spec = importlib.util.spec_from_file_location('proxys22_recovery', root / 'scripts/serve-recovery.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def get_release(args):
    if args.manifest:
        if args.manifest.stat().st_size > 65536:
            raise ReleaseError('Release manifest is too large.')
        data = json.loads(args.manifest.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('images'), dict):
            raise ReleaseError('Invalid local release manifest.')
        tag = 'v' + str(data.get('version', ''))
        assets = [{'name': image.get('name'), 'size': image.get('size'),
                   'browser_download_url': f'https://github.com/{args.repo}/releases/download/{tag}/{image.get("name")}'}
                  for image in data.get('images', {}).values() if isinstance(image, dict)]
        return parse_manifest(data, tag, assets, args.repo)
    return Catalog(args.repo).load(args.tag)


def prepare_image(args, kind):
    release = get_release(args)
    print(f'Firmware {release.tag} / Keenetic KN-1112 / {kind}')
    path = getattr(args, 'image', None)
    if path is None:
        if args.manifest:
            path = args.manifest.parent / release.images[kind].name
        else:
            path = Catalog(args.repo).download(release, kind, args.cache_dir)
    verify_image(path, release.images[kind])
    print(f'SHA-256 verified: {path}')
    return release, Path(path)


def router(args):
    return Router(args.host, args.ssh_port, args.user, args.identity, args.state_dir)


def run_command(args):
    if args.command == 'releases':
        release = get_release(args)
        print(f'{args.repo}: {release.tag} (stable)')
        for image in release.images.values():
            print(f'  {image.kind}: {image.name} / {image.size:,} bytes / SHA256 {image.sha256}')
    elif args.command == 'download':
        release = get_release(args)
        directory = args.output or args.cache_dir
        path = Catalog(args.repo).download(release, args.kind, directory)
        print(f'Verified firmware: {path}')
    elif args.command == 'flash':
        recovery = load_recovery()
        recovery.validate_bind(args.bind)
        if not 1 <= args.port <= 65535:
            raise ValueError('TFTP port must be between 1 and 65535.')
        release, path = prepare_image(args, 'factory')
        print('\nFIRST INSTALLATION / RECOVERY — Keenetic Start KN-1112 ONLY\n'
              'Save the stock settings and the official KN-1112 recovery image first.\n'
              'Connect Ethernet to LAN port 1–3; disconnect the WAN cable.\n'
              'Record the selected Ethernet adapter settings, then set 192.168.1.2/30.\n'
              'Other adapters, routes, gateway and DNS will not be changed by this tool.\n'
              'Allow this process UDP 69 and its temporary transfer port on that adapter.\n'
              'Entering the bootloader recovery mode will WRITE the supplied firmware.')
        if args.bind != '192.168.1.2' or args.port != 69:
            print('Nonstandard TFTP address/port: the documented bootloader expects 192.168.1.2:69.')
        if not confirm('Confirm hardware and backup: type KN-1112 to start the factory server: ', 'KN-1112'):
            raise DeviceError('First flash cancelled.')
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        try:
            server = recovery.RecoveryServer(path, args.bind, args.port)
        except PermissionError as exc:
            raise DeviceError('Cannot bind UDP 69. Start with the required local privileges; network/firewall settings were not changed.') from exc
        expected = release.images['factory']
        if len(server.image) != expected.size or hashlib.sha256(server.image).hexdigest() != expected.sha256:
            server.sock.close()
            raise ReleaseError('Factory image changed after verification; server was not started.')
        print('\nServer ready. With power OFF, hold Reset, connect power, then release Reset\n'
              'when Power starts blinking. Wait for transfer, writing and reboot; do not cut power.\n'
              'Transfer completion alone does not confirm that the device has booted.\n'
              'After boot press Ctrl+C here; restore only the selected adapter settings.\n'
              'For isolated setup use Ethernet 192.168.2.2/24 with no gateway or DNS.\n'
              'Set a root password using SSH before enabling Wi-Fi. See docs/recovery.md.')
        try:
            server.serve_forever()
        finally:
            server.close()
    elif args.command == 'diagnose':
        with router(args) as device:
            print(json.dumps(device.diagnostics(), ensure_ascii=False, indent=2))
    elif args.command == 'backup':
        with router(args) as device:
            print(f'Private backup verified: {device.backup(args.output or args.state_dir / "backups")}')
    elif args.command == 'update':
        release, path = prepare_image(args, 'sysupgrade')
        with router(args) as device:
            current = device.firmware().get('version', 'unknown')
            print(f'Installed: {current}; requested: {release.version}')
            if current == release.version and not args.reinstall:
                print('This firmware is already installed. Use --reinstall only for an intentional reinstall.')
                return
            if re_version(current) is not None and re_version(current) > re_version(release.version):
                raise DeviceError('Downgrade refused. Use a supported recovery procedure instead.')
            device.upgrade(path, release.images['sysupgrade'], args.backup_dir or args.state_dir / 'backups', confirm)
        print('Upgrade accepted by the device; reboot is starting. Keep power connected.\n'
              'After reboot run diagnose to verify the installed version and tunnel state.')
    elif args.command == 'doctor':
        print(f'proxys22 manager {VERSION}\nPlatform: {platform.system()} {platform.machine()}')
        print('Supported device: Keenetic Start KN-1112; other models are refused.')
        print('State/cache directory:', args.state_dir)
        print('Factory: Ethernet, manual adapter preparation, UDP 69 and local bind privileges.')
        load_recovery()
        print('Factory recovery engine: available')
        try:
            import paramiko
            print('SSH library:', paramiko.__version__)
        except ImportError:
            print('SSH unavailable: install requirements-manager.txt or use the packaged executable.')
        if args.online:
            print('Public release connection:', get_release(args).tag)


def re_version(value):
    import re
    if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+', value):
        return tuple(int(part) for part in value.split('.'))
    return None


def parser():
    root = argparse.ArgumentParser(description='proxys22 — Windows/Linux terminal firmware manager')
    root.add_argument('--version', action='version', version=VERSION)
    root.add_argument('--repo', default=DEFAULT_REPO, help='Public GitHub owner/name')
    root.add_argument('--tag', help='Pin a stable firmware release, for example v0.4.0')
    root.add_argument('--manifest', type=Path, help='Local release manifest for offline verified images')
    root.add_argument('--state-dir', type=Path, default=default_state())
    root.add_argument('--cache-dir', type=Path, help='Verified firmware download directory')
    sub = root.add_subparsers(dest='command')
    sub.add_parser('menu', help='Interactive terminal menu (also the default)')
    sub.add_parser('releases', help='Show the latest stable firmware and checksums')
    download = sub.add_parser('download', help='Download and verify an image without flashing')
    download.add_argument('--kind', choices=('factory', 'sysupgrade'), required=True)
    download.add_argument('--output', type=Path)
    flash = sub.add_parser('flash', help='Guided first installation / factory TFTP server')
    flash.add_argument('--bind', required=True, help='Wired PC adapter IPv4 address')
    flash.add_argument('--port', type=int, default=69)
    flash.add_argument('--image', type=Path, help='Verify and use a local factory image')
    for name in ('diagnose', 'backup', 'update'):
        command = sub.add_parser(name, help={'diagnose': 'Read-only router diagnostics',
                                           'backup': 'Download and verify a private settings backup',
                                           'update': 'Backup, verify and install a sysupgrade image'}[name])
        command.add_argument('--host', required=True, help='Router IP address or hostname')
        command.add_argument('--ssh-port', type=int, default=22)
        command.add_argument('--user', default='root')
        command.add_argument('--identity', type=Path, help='SSH private key; never uploaded')
        if name == 'backup':
            command.add_argument('--output', type=Path)
        if name == 'update':
            command.add_argument('--image', type=Path)
            command.add_argument('--backup-dir', type=Path)
            command.add_argument('--reinstall', action='store_true')
    doctor = sub.add_parser('doctor', help='Check this computer and optionally GitHub connectivity')
    doctor.add_argument('--online', action='store_true')
    return root


def menu(args):
    while True:
        print(f'\nproxys22 {VERSION} — Keenetic Start KN-1112\n'
              '1  Latest stable firmware\n2  Download a verified firmware image\n'
              '3  First flash / factory recovery\n4  Upgrade installed firmware\n'
              '5  Router diagnostics\n6  Private settings backup\n7  Check this computer\n0  Exit')
        choice = input('Select: ').strip()
        if choice == '0':
            return
        if choice not in ('1', '2', '3', '4', '5', '6', '7'):
            continue
        selected = argparse.Namespace(**vars(args))
        selected.command = {'1': 'releases', '2': 'download', '3': 'flash', '4': 'update',
                            '5': 'diagnose', '6': 'backup', '7': 'doctor'}[choice]
        selected.output = selected.image = selected.backup_dir = None
        selected.reinstall = selected.online = False
        if choice == '2':
            selected.kind = input('Image type [sysupgrade/factory], default sysupgrade: ').strip() or 'sysupgrade'
        if choice == '3':
            selected.bind = input('Wired PC adapter address [192.168.1.2]: ').strip() or '192.168.1.2'
            selected.port = 69
        if choice in ('4', '5', '6'):
            selected.host = input('Router address [192.168.2.1]: ').strip() or '192.168.2.1'
            selected.ssh_port, selected.user, selected.identity = 22, 'root', None
        try:
            run_command(selected)
        except (ReleaseError, DeviceError, OSError, ValueError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
        except KeyboardInterrupt:
            print('\nOperation stopped. If device writing has started, keep its power connected.')


def main(argv=None):
    args = parser().parse_args(argv)
    args.cache_dir = args.cache_dir or args.state_dir / 'cache'
    try:
        if args.command in (None, 'menu'):
            menu(args)
        else:
            run_command(args)
        return 0
    except (ReleaseError, DeviceError, OSError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nStopped. If firmware writing has started, keep device power connected.', file=sys.stderr)
        return 130
