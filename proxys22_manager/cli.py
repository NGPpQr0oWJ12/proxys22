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
from .ui import Console, size_text

console = Console()
IMAGE_NAMES = {'factory': 'первичная прошивка', 'sysupgrade': 'обновление системы'}


def default_state():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'proxys22'
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'proxys22'


def confirm(prompt, expected):
    return console.confirm(prompt, expected)


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
            raise ReleaseError('Манифест релиза слишком большой.')
        data = json.loads(args.manifest.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('images'), dict):
            raise ReleaseError('Неверный формат локального манифеста релиза.')
        tag = 'v' + str(data.get('version', ''))
        assets = [{'name': image.get('name'), 'size': image.get('size'),
                   'browser_download_url': f'https://github.com/{args.repo}/releases/download/{tag}/{image.get("name")}'}
                  for image in data.get('images', {}).values() if isinstance(image, dict)]
        return parse_manifest(data, tag, assets, args.repo)
    console.line('Проверяем стабильный релиз на GitHub…')
    return Catalog(args.repo).load(args.tag)


def download_image(args, release, kind, directory):
    try:
        return Catalog(args.repo).download(release, kind, directory,
                                           progress=console.progress('Загрузка образа'))
    finally:
        console.end_progress()


def prepare_image(args, kind):
    release = get_release(args)
    console.panel('ОБРАЗ ПРОШИВКИ', [f'Версия: {release.tag}', 'Устройство: Keenetic Start KN-1112',
                                      f'Назначение: {IMAGE_NAMES[kind]}'])
    path = getattr(args, 'image', None)
    if path is None:
        if args.manifest:
            path = args.manifest.parent / release.images[kind].name
        else:
            path = download_image(args, release, kind, args.cache_dir)
    verify_image(path, release.images[kind])
    console.success('Размер и контрольная сумма SHA-256 совпадают.')
    console.line('Файл: ' + str(path))
    return release, Path(path)


def router(args):
    number = 0

    def stage(message):
        nonlocal number
        number += 1
        console.step(number, 7, message)

    if not getattr(args, 'json', False):
        console.heading('Подключение к роутеру по SSH')
    return Router(args.host, args.ssh_port, args.user, args.identity, args.state_dir,
                  on_stage=stage, progress=console.progress('Передача на роутер'))


def show_diagnostics(values):
    translations = {'ethernet': 'Ethernet (кабель)', 'wifi': 'Wi-Fi', 'direct': 'Напрямую',
                    'proxy': 'Через туннель', 'amneziawg': 'AmneziaWG', 'hysteria': 'Hysteria 2',
                    'healthy': 'Соединение работает', 'checking': 'Проверяем подключение',
                    'unavailable': 'Недоступно', 'disabled': 'Выключено',
                    'no_profile': 'Профиль отсутствует', 'stopped': 'Процесс остановлен',
                    'starting': 'Запускается', 'recovering': 'Восстанавливаем соединение',
                    'unconfigured': 'Профиль не настроен', 'client_stopped': 'Клиент остановлен',
                    'client_restarting': 'Клиент перезапускается', 'probe_failed': 'Проверка связи не пройдена',
                    'dependency_missing': 'Необходимый компонент недоступен', 'route_error': 'Ошибка маршрутизации'}
    labels = [('model', 'Устройство'), ('firmware', 'Прошивка'), ('openwrt', 'Версия OpenWrt'),
              ('uptime', 'Время работы, секунд'), ('memory_total', 'Оперативная память'),
              ('memory_free', 'Свободная память'), ('uplink_mode', 'Источник Интернета'),
              ('uplink_connected', 'Интернет подключён'), ('protocol', 'Протокол'),
              ('configured', 'Профиль настроен'), ('enabled', 'Туннель включён'),
              ('running', 'Процесс туннеля работает'), ('route', 'Текущий маршрут'),
              ('healthy', 'Туннель доступен'), ('reason', 'Состояние соединения')]
    console.heading('Диагностика устройства')
    for key, label in labels:
        value = values.get(key)
        if value is None:
            rendered = 'Нет данных'
        elif isinstance(value, bool):
            rendered = 'Да' if value else 'Нет'
        elif key.startswith('memory_') and isinstance(value, (int, float)):
            rendered = size_text(value)
        else:
            rendered = translations.get(value, value)
        console.line(f'  {label}: {rendered}')


class RecoveryLog(logging.Handler):
    def emit(self, record):
        console.line(record.getMessage())


def run_command(args):
    if args.command == 'releases':
        release = get_release(args)
        console.panel('СТАБИЛЬНЫЙ РЕЛИЗ ' + release.tag, ['Устройство: Keenetic Start KN-1112', args.repo])
        for image in release.images.values():
            console.heading(IMAGE_NAMES[image.kind].capitalize())
            console.line(f'  Размер: {size_text(image.size)}')
            console.line(f'  Файл: {image.name}')
            console.line(f'  SHA-256: {image.sha256}')
    elif args.command == 'download':
        release = get_release(args)
        directory = args.output or args.cache_dir
        path = download_image(args, release, args.kind, directory)
        console.success('Образ загружен и проверен по SHA-256.')
        console.line('Файл: ' + str(path))
    elif args.command == 'flash':
        recovery = load_recovery()
        recovery.validate_bind(args.bind)
        if not 1 <= args.port <= 65535:
            raise ValueError('Порт TFTP должен быть от 1 до 65535.')
        release, path = prepare_image(args, 'factory')
        console.panel('ПЕРВИЧНАЯ ПРОШИВКА / ВОССТАНОВЛЕНИЕ', [
            'Поддерживается только Keenetic Start KN-1112.',
            'Сохраните штатные настройки и официальный образ возврата KeeneticOS.',
            'Подключите кабель к LAN 1–3 и отключите WAN.',
            'Запишите настройки проводного адаптера и задайте ему 192.168.1.2/30.',
            'Разрешите процессу входящий UDP 69 и временный порт передачи на этом адаптере.',
            'Вход в режим восстановления запустит запись выбранной прошивки.'])
        if args.bind != '192.168.1.2' or args.port != 69:
            console.warning('Выбран нестандартный адрес или порт; загрузчик ожидает 192.168.1.2:69.')
        if not confirm('Проверьте модель и резервную копию. Для запуска сервера введите ПРОШИТЬ KN-1112; Enter — отмена.', 'ПРОШИТЬ KN-1112'):
            raise DeviceError('Первичная прошивка отменена.')
        handler = RecoveryLog()
        previous_level, previous_propagate = recovery.LOG.level, recovery.LOG.propagate
        recovery.LOG.addHandler(handler)
        recovery.LOG.setLevel(logging.INFO)
        recovery.LOG.propagate = False
        try:
            try:
                server = recovery.RecoveryServer(path, args.bind, args.port,
                                                  progress=console.progress('Передача загрузчику'))
            except PermissionError as exc:
                raise DeviceError('Не удалось открыть UDP 69. Нужны соответствующие локальные права.') from exc
            expected = release.images['factory']
            if len(server.image) != expected.size or hashlib.sha256(server.image).hexdigest() != expected.sha256:
                server.sock.close()
                raise ReleaseError('Образ изменился после проверки. Сервер остановлен.')
            console.success('Сервер готов. Ожидаем запрос от загрузчика.')
            console.line('При выключенном питании удерживайте Reset, включите питание, отпустите Reset при мигании Power.')
            console.warning('Дождитесь передачи, записи и перезагрузки. Не отключайте питание и кабель.')
            console.line('100% передачи означает доставку файла, а не завершение записи или загрузки ОС.')
            console.line('После загрузки нажмите Ctrl+C. Восстановите настройки выбранного адаптера.')
            console.line('Для изолированной настройки: Ethernet 192.168.2.2/24, шлюз и DNS пустые.')
            console.line('Перед включением Wi-Fi задайте пароль root по SSH. Подробнее: docs/recovery.md.')
            try:
                server.serve_forever()
            finally:
                server.close()
        finally:
            recovery.LOG.removeHandler(handler)
            recovery.LOG.setLevel(previous_level)
            recovery.LOG.propagate = previous_propagate
            console.end_progress()
    elif args.command == 'diagnose':
        with router(args) as device:
            values = device.diagnostics()
        if args.json:
            print(json.dumps(values, ensure_ascii=False, indent=2))
        else:
            show_diagnostics(values)
    elif args.command == 'backup':
        with router(args) as device:
            console.heading('Создание и проверка резервной копии')
            path = device.backup(args.output or args.state_dir / 'backups')
        console.success('Архив резервной копии проверен.')
        console.line('Закрытая копия: ' + str(path))
    elif args.command == 'update':
        release, path = prepare_image(args, 'sysupgrade')
        with router(args) as device:
            current = device.firmware().get('version', 'неизвестно')
            console.panel('ПОДГОТОВКА ОБНОВЛЕНИЯ', [f'Установлена версия: {current}',
                f'Выбрана версия: {release.version}', 'Настройки и профили сохраняются.',
                'Запись начнётся после всех проверок и отдельного подтверждения ОБНОВИТЬ.'])
            if current == release.version and not args.reinstall:
                console.success('Эта версия уже установлена. Для намеренной переустановки добавьте --reinstall.')
                return
            if re_version(current) is not None and re_version(current) > re_version(release.version):
                raise DeviceError('Понижение версии запрещено. Используйте процедуру восстановления.')
            try:
                device.upgrade(path, release.images['sysupgrade'], args.backup_dir or args.state_dir / 'backups', confirm)
            finally:
                console.end_progress()
        console.success('Роутер принял команду обновления. Начинается перезагрузка.')
        console.warning('Сохраняйте питание. После загрузки проверьте версию и туннель командой diagnose.')
    elif args.command == 'doctor':
        console.heading('Проверка компьютера')
        console.line(f'  Система: {platform.system()} {platform.machine()}')
        console.line('  Поддерживаемое устройство: Keenetic Start KN-1112')
        console.line('  Каталог состояния: ' + str(args.state_dir))
        console.line('  Первичная прошивка: Ethernet, подготовленный адаптер и доступ к UDP 69')
        load_recovery()
        console.success('Сервер первичной прошивки включён в программу.')
        try:
            import paramiko
            console.success('Библиотека SSH доступна: ' + paramiko.__version__)
        except ImportError:
            console.warning('SSH недоступен. Установите requirements-manager.txt или используйте готовую программу.')
        if args.online:
            console.success('GitHub доступен. Стабильный релиз: ' + get_release(args).tag)


def re_version(value):
    import re
    if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+', value):
        return tuple(int(part) for part in value.split('.'))
    return None


class RussianFormatter(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(usage, actions, groups, 'Использование: ' if prefix is None else prefix)


class RussianParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('formatter_class', RussianFormatter)
        kwargs['add_help'] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = 'Команды'
        self._optionals.title = 'Параметры'
        self.add_argument('-h', '--help', action='help', help='Показать справку и выйти')

    def error(self, message):
        for original, translated in [
            ('the following arguments are required:', 'не указаны обязательные параметры:'),
            ('unrecognized arguments:', 'неизвестные параметры:'), ('invalid choice:', 'недопустимое значение:'),
            ('choose from', 'выберите из'), ('expected one argument', 'ожидается одно значение'),
            ('invalid int value:', 'ожидается целое число:'), ('argument ', 'параметр '),
        ]:
            message = message.replace(original, translated)
        self.print_usage(sys.stderr)
        self.exit(2, 'Ошибка: ' + message + '\n')


def parser():
    root = RussianParser(description='proxys22 — терминальное управление прошивкой для Windows и Linux')
    root.add_argument('--version', action='version', version=VERSION, help='Показать версию и выйти')
    root.add_argument('--repo', default=DEFAULT_REPO, help='Публичный репозиторий GitHub: владелец/имя')
    root.add_argument('--tag', help='Выбрать конкретный стабильный релиз, например v0.4.1')
    root.add_argument('--manifest', type=Path, help='Локальный манифест для проверки образов без Интернета')
    root.add_argument('--state-dir', type=Path, default=default_state(), help='Каталог состояния и закрытых копий')
    root.add_argument('--cache-dir', type=Path, help='Каталог проверенных образов прошивки')
    sub = root.add_subparsers(dest='command')
    sub.add_parser('menu', help='Интерактивное меню; открывается и без аргументов')
    sub.add_parser('releases', help='Стабильная прошивка и контрольные суммы')
    download = sub.add_parser('download', help='Скачать и проверить образ без прошивки устройства')
    download.add_argument('--kind', choices=('factory', 'sysupgrade'), required=True, help='factory — первая прошивка; sysupgrade — обновление')
    download.add_argument('--output', type=Path, help='Каталог для загруженного файла')
    flash = sub.add_parser('flash', help='Первичная прошивка и восстановление через TFTP')
    flash.add_argument('--bind', required=True, help='IPv4-адрес проводного адаптера компьютера')
    flash.add_argument('--port', type=int, default=69, help='Порт TFTP; по умолчанию 69')
    flash.add_argument('--image', type=Path, help='Проверить и использовать локальный образ factory')
    for name in ('diagnose', 'backup', 'update'):
        command = sub.add_parser(name, help={'diagnose': 'Диагностика устройства',
                                           'backup': 'Закрытая резервная копия настроек',
                                           'update': 'Резервная копия, проверка и обновление прошивки'}[name])
        command.add_argument('--host', required=True, help='IP-адрес или имя роутера')
        command.add_argument('--ssh-port', type=int, default=22, help='Порт SSH; по умолчанию 22')
        command.add_argument('--user', default='root', help='Пользователь SSH; по умолчанию root')
        command.add_argument('--identity', type=Path, help='Локальный закрытый ключ SSH')
        if name == 'diagnose':
            command.add_argument('--json', action='store_true', help='Исходный машинный отчёт в формате JSON')
        if name == 'backup':
            command.add_argument('--output', type=Path, help='Каталог для закрытой резервной копии')
        if name == 'update':
            command.add_argument('--image', type=Path, help='Локальный образ sysupgrade')
            command.add_argument('--backup-dir', type=Path, help='Каталог для закрытой резервной копии')
            command.add_argument('--reinstall', action='store_true', help='Разрешить переустановку той же версии')
    doctor = sub.add_parser('doctor', help='Проверка компьютера и доступа к релизам')
    doctor.add_argument('--online', action='store_true', help='Дополнительно проверить доступ к GitHub')
    return root


def describe_error(error):
    if isinstance(error, FileNotFoundError):
        return 'Файл или каталог не найден' + (': ' + str(error.filename) if error.filename else '.')
    if isinstance(error, PermissionError):
        return 'Недостаточно прав для выбранного файла, каталога или сетевого порта.'
    if isinstance(error, json.JSONDecodeError):
        return 'Некорректный JSON в манифесте или ответе устройства.'
    if isinstance(error, OSError):
        return 'Не удалось выполнить операцию с файлом или сетью. Проверьте путь, права и подключение.'
    return str(error)


def menu(args):
    while True:
        console.panel(f'proxys22 {VERSION}', ['Keenetic Start KN-1112', 'Прошивка · Обновление · Диагностика'])
        for row in ['  1  Последняя стабильная прошивка', '  2  Скачать и проверить образ',
                    '  3  Первичная прошивка / восстановление', '  4  Обновить прошивку роутера',
                    '  5  Диагностика роутера', '  6  Резервная копия настроек',
                    '  7  Проверить этот компьютер', '', '  0  Выход']:
            console.line(row)
        choice = console.prompt('Выберите действие')
        if choice == '0':
            return
        if choice not in ('1', '2', '3', '4', '5', '6', '7'):
            console.warning('Введите номер действия от 0 до 7.')
            continue
        selected = argparse.Namespace(**vars(args))
        selected.command = {'1': 'releases', '2': 'download', '3': 'flash', '4': 'update',
                            '5': 'diagnose', '6': 'backup', '7': 'doctor'}[choice]
        selected.output = selected.image = selected.backup_dir = None
        selected.reinstall = selected.online = selected.json = False
        if choice == '2':
            kind = console.prompt('Тип образа: 1 — обновление системы; 2 — первичная прошивка', '1')
            if kind not in ('1', '2'):
                console.warning('Выберите тип образа: 1 или 2.')
                continue
            selected.kind = 'factory' if kind == '2' else 'sysupgrade'
        if choice == '3':
            selected.bind = console.prompt('IPv4-адрес проводного адаптера компьютера', '192.168.1.2')
            selected.port = 69
        if choice in ('4', '5', '6'):
            selected.host = console.prompt('Адрес роутера', '192.168.2.1')
            selected.ssh_port, selected.user, selected.identity = 22, 'root', None
        try:
            run_command(selected)
        except (ReleaseError, DeviceError, OSError, ValueError) as exc:
            console.error(describe_error(exc))
        except KeyboardInterrupt:
            console.warning('Операция остановлена. Если запись уже началась, сохраняйте питание роутера.')
        console.prompt('Нажмите Enter, чтобы вернуться в меню')


def main(argv=None):
    global console
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure') and not stream.isatty():
            stream.reconfigure(encoding='utf-8', errors='replace')
    console = Console()
    args = parser().parse_args(argv)
    args.cache_dir = args.cache_dir or args.state_dir / 'cache'
    try:
        if args.command in (None, 'menu'):
            menu(args)
        else:
            if not (args.command == 'diagnose' and args.json):
                console.panel(f'proxys22 {VERSION}', ['Keenetic Start KN-1112'])
            run_command(args)
        return 0
    except (ReleaseError, DeviceError, OSError, ValueError) as exc:
        console.error(describe_error(exc))
        return 1
    except (KeyboardInterrupt, EOFError):
        console.warning('Ввод завершён. Если запись уже началась, сохраняйте питание роутера.')
        return 130
