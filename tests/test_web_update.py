"""Offline updater contracts and real ucode execution in disposable fixtures."""

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
UCODE = Path('/opt/wifi-proxy/ucode/build-host/ucode')
MODULES = '/opt/wifi-proxy/ucode/build-host'
REMOTE = 'NGPpQr0oWJ12/proxys22'
VERSION = '0.5.0'
TAG = 'v' + VERSION
PAYLOAD = b'offline web updater firmware fixture\x00\xff'
REQUIRED = ['/etc/config/network', '/etc/config/wireless', '/etc/wifi-proxy/initialized',
            '/etc/wifi-proxy/client.json', '/etc/wifi-proxy/awg.json']
BOARD = {'board_name': 'keenetic,kn-1112', 'release': {'target': 'ramips/mt76x8'}}


def archive_bytes(names=REQUIRED):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name in names:
            body = b'' if name.endswith('/initialized') else b'fixture settings\n'
            member = tarfile.TarInfo(name.lstrip('/'))
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


def release_fixture(version=VERSION):
    tag = 'v' + version
    images, assets = {}, []
    for kind in ('factory', 'sysupgrade'):
        name = f'proxys22-{version}-keenetic_kn-1112-{kind}.bin'
        images[kind] = {'name': name, 'size': len(PAYLOAD), 'sha256': hashlib.sha256(PAYLOAD).hexdigest()}
        assets.append({'name': name, 'size': len(PAYLOAD),
                       'browser_download_url': f'https://github.com/{REMOTE}/releases/download/{tag}/{name}'})
    manifest_url = f'https://github.com/{REMOTE}/releases/download/{tag}/proxys22-release.json'
    assets.append({'name': 'proxys22-release.json', 'browser_download_url': manifest_url})
    return ({'tag_name': tag, 'draft': False, 'prerelease': False, 'assets': assets},
            {'schema': 1, 'brand': 'proxys22', 'version': version, 'profile': 'keenetic_kn-1112',
             'target': 'ramips/mt76x8', 'images': images})


STUB = r'''
import base64, hashlib, json, os, pathlib, subprocess, sys
fixture = json.loads(pathlib.Path(os.environ['WEB_UPDATE_FIXTURE']).read_text())
root = pathlib.Path(fixture['root'])
kind = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(fixture['calls'], 'a') as log:
    log.write(json.dumps([kind, *args]) + '\n')
if kind == 'curl':
    if fixture.get('curl_failure'):
        sys.exit(22)
    output = pathlib.Path(args[args.index('--output') + 1])
    url = args[-1]
    payload = fixture['downloads'].get(url)
    if payload is None:
        sys.exit(23)
    output.write_bytes(base64.b64decode(payload))
    sys.exit(0)
action = args[0]
if action.startswith('start-'):
    sys.exit(fixture.get('start_failure', 0))
if action == 'board':
    print(json.dumps(fixture['board']))
elif action == 'settings-hash':
    for name in fixture['required']:
        path = pathlib.Path(fixture['etc']) / name.lstrip('/')
        if path.is_file():
            print(hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + name)
elif action == 'backup':
    (root / 'backup.tar.gz').write_bytes(base64.b64decode(fixture['backup']))
elif action == 'verify-backup':
    if subprocess.run(['/bin/gzip', '-t', str(root / 'backup.tar.gz')], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode:
        sys.exit(1)
    with (root / 'backup.list').open('wb') as output:
        result = subprocess.run(['/bin/tar', '-tvzf', str(root / 'backup.tar.gz')], stdout=output,
                                stderr=subprocess.DEVNULL)
    sys.exit(result.returncode)
elif action == 'test-image':
    sys.exit(fixture.get('test_failure', 0))
elif action == 'flash':
    (root / 'flash-stub-called').write_text('Only an offline stub was invoked.\n')
else:
    sys.exit(99)
'''


class AuthSchemaTests(unittest.TestCase):
    def test_update_privileges_and_rpc_argument_schemas_are_explicit(self):
        acl = json.loads((REPO / 'overlay/usr/share/rpcd/acl.d/wifi-proxy.json').read_text())['wifi-proxy']
        read = acl['read']['ubus']['wifi_proxy']
        write = acl['write']['ubus']['wifi_proxy']
        self.assertIn('update_check', read)
        self.assertIn('update_status', read)
        for name in ('update_prepare', 'update_backup', 'update_apply'):
            self.assertIn(name, write)
            self.assertNotIn(name, read)
        source = (REPO / 'overlay/usr/libexec/rpcd/wifi_proxy').read_text()
        self.assertIn("update_prepare: { version: '' }", source)
        self.assertIn("update_backup: { job_id: '' }", source)
        self.assertIn("update_apply: { job_id: '', confirmation: '', backup_saved: false }", source)


@unittest.skipUnless(os.name == 'posix' and UCODE.is_file(), 'Requires the isolated Linux/WSL ucode runtime')
class WebUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='web-update-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / 'runtime'
        self.etc = self.directory / 'filesystem'
        self.marker = self.directory / 'marker.json'
        self.marker.write_text(json.dumps({'name': 'proxys22', 'version': '0.4.1'}))
        for name in REQUIRED:
            path = self.etc / name.lstrip('/')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture settings\n')
        self.helper = self.directory / 'helper'
        self.curl = self.directory / 'curl'
        for executable in (self.helper, self.curl):
            executable.write_text(f'#!{sys.executable}\n' + STUB)
            executable.chmod(0o755)
        self.fixture_path = self.directory / 'fixture.json'
        self.metadata, self.manifest = release_fixture()
        self.fixture = {'root': str(self.root), 'etc': str(self.etc), 'calls': str(self.directory / 'calls.jsonl'),
                        'board': copy.deepcopy(BOARD), 'required': REQUIRED, 'backup': base64.b64encode(archive_bytes()).decode()}
        self.sync_fixture()
        source = (REPO / 'overlay/usr/share/wifi-proxy/update.uc').read_text()
        source = source.replace('/tmp/proxys22-update', str(self.root))
        source = source.replace('/usr/libexec/wifi-proxy/update', str(self.helper))
        source = source.replace('/usr/bin/curl', str(self.curl))
        source = source.replace('/etc/proxys22-release', str(self.marker))
        source = source.replace('/bin/ubus -t 5 call system board', str(self.helper) + ' board')
        source = source.replace('stat(path)', "stat('" + str(self.etc) + "' + path)")
        self.module = self.directory / 'update.uc'
        self.module.write_text(source)

    def sync_fixture(self):
        manifest_url = self.metadata['assets'][-1]['browser_download_url']
        image_url = self.metadata['assets'][1]['browser_download_url']
        downloads = {
            f'https://api.github.com/repos/{REMOTE}/releases/latest': json.dumps(self.metadata).encode(),
            f'https://api.github.com/repos/{REMOTE}/releases/tags/{TAG}': json.dumps(self.metadata).encode(),
            manifest_url: json.dumps(self.manifest).encode(), image_url: PAYLOAD,
        }
        self.fixture['downloads'] = {url: base64.b64encode(body).decode() for url, body in downloads.items()}
        self.fixture_path.write_text(json.dumps(self.fixture))

    def evaluate(self, expression):
        source = (f"import {{ update_dispatch, run_update, validate_release, validate_board_info, compare_versions }} from '{self.module}';\n"
                  "try { printf('%J', " + expression + "); } catch (e) { printf('%J', { ok: false, message: e.message }); }\n")
        result = subprocess.run([str(UCODE), '-L', MODULES, '-e', source], text=True, capture_output=True,
                                env={**os.environ, 'WEB_UPDATE_FIXTURE': str(self.fixture_path)}, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def call(self, method, arguments=None):
        return self.evaluate(f'update_dispatch({json.dumps(method)}, {json.dumps(arguments or {})})')

    def worker(self, mode):
        return self.evaluate(f'run_update([{json.dumps(mode)}])')

    def status(self):
        return self.call('update_status')

    def calls(self):
        path = Path(self.fixture['calls'])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def ready(self):
        check = self.call('update_check')
        self.assertTrue(check['ok'], check)
        self.assertEqual(self.worker('check'), 0, self.status())
        self.assertEqual(self.status()['phase'], 'available')
        self.assertTrue(self.call('update_prepare', {'version': VERSION})['ok'])
        self.assertEqual(self.worker('prepare'), 0, self.status())
        self.assertEqual(self.status()['phase'], 'ready')
        return check['job_id']

    def download_backup(self, job):
        result = self.call('update_backup', {'job_id': job})
        self.assertTrue(result['ok'], result)
        self.assertEqual(base64.b64decode(result['base64']), base64.b64decode(self.fixture['backup']))
        return result

    def test_pinned_manifest_url_board_target_size_hash_and_versions(self):
        self.assertEqual(self.evaluate(f'validate_release({json.dumps(self.metadata)}, {json.dumps(self.manifest)}, "0.4.1", "{TAG}")')['version'], VERSION)
        mutations = [
            ('foreign URL', lambda meta, data: meta['assets'][1].update(browser_download_url='https://evil.invalid/image.bin')),
            ('wrong tag', lambda meta, data: meta.update(tag_name='v0.9.0')),
            ('wrong board', lambda meta, data: data.update(profile='keenetic_kn-1110')),
            ('wrong target', lambda meta, data: data.update(target='ramips/mt7621')),
            ('boolean size', lambda meta, data: data['images']['sysupgrade'].update(size=True)),
            ('oversize', lambda meta, data: data['images']['sysupgrade'].update(size=16121857)),
            ('bad hash', lambda meta, data: data['images']['sysupgrade'].update(sha256='A' * 64)),
            ('traversal name', lambda meta, data: data['images']['sysupgrade'].update(name='../image.bin')),
            ('draft', lambda meta, data: meta.update(draft=True)),
            ('prerelease', lambda meta, data: meta.update(prerelease=True)),
        ]
        for name, mutate in mutations:
            with self.subTest(case=name):
                metadata, manifest = copy.deepcopy(self.metadata), copy.deepcopy(self.manifest)
                mutate(metadata, manifest)
                result = self.evaluate(f'validate_release({json.dumps(metadata)}, {json.dumps(manifest)}, "0.4.1", "{TAG}")')
                self.assertFalse(result['ok'])
        result = self.evaluate(f'validate_release({json.dumps(self.metadata)}, {json.dumps(self.manifest)}, "0.9.0", "{TAG}")')
        self.assertFalse(result['ok'])
        for board in ({'board_name': 'other'}, {'board_name': 'keenetic,kn-1112', 'release': {'target': 'other'}}):
            self.assertFalse(self.evaluate('validate_board_info(' + json.dumps(board) + ')')['ok'])

    def test_check_is_async_cached_and_reports_installed_current_version(self):
        check = self.call('update_check')
        self.assertEqual(self.status()['phase'], 'checking')
        self.assertFalse(any(row[0] == 'curl' for row in self.calls()))
        self.assertEqual(self.call('update_check'), check)
        self.assertEqual(self.worker('check'), 0)
        self.assertEqual(self.status()['installed_version'], '0.4.1')
        self.assertEqual(self.call('update_check'), check)
        self.assertEqual(sum(row == ['helper', 'start-check'] for row in self.calls()), 1)

    def test_same_version_is_current_and_cannot_prepare(self):
        self.marker.write_text(json.dumps({'name': 'proxys22', 'version': VERSION}))
        self.call('update_check')
        self.assertEqual(self.worker('check'), 0)
        self.assertEqual(self.status()['phase'], 'current')
        self.assertFalse(self.call('update_prepare', {'version': VERSION})['ok'])

    def test_preparation_is_private_verified_and_never_flashes(self):
        self.ready()
        state = self.status()
        self.assertEqual((state['received_bytes'], state['size']), (len(PAYLOAD), len(PAYLOAD)))
        self.assertFalse(state['backup_downloaded'])
        self.assertFalse((self.root / 'flash-stub-called').exists())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'state.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'image.bin').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'backup.tar.gz').stat().st_mode & 0o777, 0o600)
        for key in ('base64', 'image', 'required', 'settings_hash', 'url', 'sha256'):
            self.assertNotIn(key, state)
        for row in self.calls():
            if row[0] == 'curl':
                self.assertIn('--proto', row)
                self.assertIn('--proto-redir', row)
                self.assertIn('=https', row)
                self.assertNotIn('--insecure', row)
        self.assertIn(['helper', 'test-image'], self.calls())

    def test_ready_backup_and_explicit_confirmation_are_all_required(self):
        job = self.ready()
        valid = {'job_id': job, 'confirmation': 'ОБНОВИТЬ', 'backup_saved': True}
        self.assertFalse(self.call('update_apply', valid)['ok'])
        self.download_backup(job)
        for change in ({'job_id': 'wrong'}, {'confirmation': ''}, {'confirmation': 'да'}, {'backup_saved': False}):
            with self.subTest(change=change):
                arguments = {**valid, **change}
                self.assertFalse(self.call('update_apply', arguments)['ok'])
        self.assertFalse((self.root / 'flash-stub-called').exists())
        self.assertEqual(self.status()['phase'], 'ready')
        self.assertTrue(self.call('update_apply', valid)['ok'])
        self.assertEqual(self.status()['phase'], 'upgrading')
        self.assertEqual(sum(row == ['helper', 'start-apply'] for row in self.calls()), 1)

    def test_apply_rechecks_before_ack_and_repeated_job_never_schedules_twice(self):
        job = self.ready()
        self.download_backup(job)
        arguments = {'job_id': job, 'confirmation': 'ОБНОВИТЬ', 'backup_saved': True}
        before = len(self.calls())
        accepted = self.call('update_apply', arguments)
        self.assertTrue(accepted['ok'], accepted)
        after = self.calls()[before:]
        self.assertLess(after.index(['helper', 'test-image']), after.index(['helper', 'start-apply']))
        self.assertEqual(self.call('update_apply', arguments), accepted)
        self.assertEqual(sum(row == ['helper', 'start-apply'] for row in self.calls()), 1)
        self.assertFalse((self.root / 'flash-stub-called').exists())
        self.assertEqual(self.worker('apply'), 0)
        self.assertTrue((self.root / 'flash-stub-called').exists())
        self.assertEqual(self.status()['phase'], 'upgrading')

    def test_tampered_image_and_changed_settings_prevent_apply(self):
        for changed in ('image', 'profile', 'board', 'compatibility'):
            with self.subTest(changed=changed):
                if changed != 'image':
                    self.tearDown()
                    self.setUp()
                job = self.ready()
                self.download_backup(job)
                if changed == 'image':
                    (self.root / 'image.bin').write_bytes(b'x' * len(PAYLOAD))
                elif changed == 'profile':
                    (self.etc / 'etc/wifi-proxy/awg.json').write_text('changed fixture profile')
                elif changed == 'board':
                    self.fixture['board']['board_name'] = 'other'
                    self.sync_fixture()
                else:
                    self.fixture['test_failure'] = 1
                    self.sync_fixture()
                result = self.call('update_apply', {'job_id': job, 'confirmation': 'ОБНОВИТЬ', 'backup_saved': True})
                self.assertFalse(result['ok'])
                self.assertNotIn(['helper', 'start-apply'], self.calls())
                self.assertFalse((self.root / 'lock').exists())
                state = self.status()
                self.assertEqual(state['phase'], 'error')
                self.assertFalse(state['backup_downloaded'])
                self.assertEqual(state['message'], state['stage'])
                self.assertLessEqual(len(state['message']), 280)
                self.fixture['board'] = copy.deepcopy(BOARD)
                self.fixture['test_failure'] = 0
                self.sync_fixture()
                fresh = self.ready()
                self.assertNotEqual(fresh, job)
                self.assertFalse(self.status()['backup_downloaded'])

    def test_corrupt_ready_backup_allows_immediate_new_check_and_preparation(self):
        for method in ('update_backup', 'update_apply'):
            with self.subTest(method=method):
                if method != 'update_backup':
                    self.tearDown()
                    self.setUp()
                job = self.ready()
                if method == 'update_apply':
                    self.download_backup(job)
                backup = self.root / 'backup.tar.gz'
                backup.write_bytes(backup.read_bytes()[:-8])
                arguments = {'job_id': job, 'confirmation': 'ОБНОВИТЬ', 'backup_saved': True}
                self.assertFalse(self.call(method, arguments)['ok'])
                state = self.status()
                self.assertEqual(state['phase'], 'error')
                self.assertFalse(state['backup_downloaded'])
                self.assertEqual(state['message'], state['stage'])
                self.assertFalse((self.root / 'lock').exists())
                self.assertNotIn(['helper', 'start-apply'], self.calls())
                self.assertNotEqual(self.ready(), job)

    def test_backup_completeness_and_gzip_crc_are_verified(self):
        for invalid in ('missing profile', 'missing trailer', 'bad CRC'):
            with self.subTest(invalid=invalid):
                if invalid != 'missing profile':
                    self.tearDown()
                    self.setUp()
                body = archive_bytes()
                if invalid == 'missing profile':
                    body = archive_bytes([name for name in REQUIRED if not name.endswith('/awg.json')])
                elif invalid == 'missing trailer':
                    body = body[:-8]
                else:
                    body = body[:-8] + bytes([body[-8] ^ 1]) + body[-7:]
                self.fixture['backup'] = base64.b64encode(body).decode()
                self.sync_fixture()
                self.call('update_check')
                self.assertEqual(self.worker('check'), 0)
                self.call('update_prepare', {'version': VERSION})
                self.assertEqual(self.worker('prepare'), 1)
                self.assertEqual(self.status()['phase'], 'error')
                self.assertFalse((self.root / 'lock').exists())
                self.assertNotIn(['helper', 'flash'], self.calls())

    def test_failed_download_releases_lock_and_returns_bounded_error_state(self):
        self.fixture['curl_failure'] = True
        self.sync_fixture()
        self.call('update_check')
        self.assertEqual(self.worker('check'), 1)
        state = self.status()
        self.assertEqual(state['phase'], 'error')
        self.assertLess(len(state['message']), 300)
        self.assertNotIn('error', state)
        self.assertFalse((self.root / 'lock').exists())

    def test_live_download_bytes_come_from_staged_file_and_wrong_board_stops_network(self):
        self.call('update_check')
        self.assertEqual(self.worker('check'), 0)
        self.call('update_prepare', {'version': VERSION})
        (self.root / 'image.bin').write_bytes(PAYLOAD[:9])
        state = self.status()
        self.assertEqual((state['phase'], state['received_bytes'], state['size']), ('preparing', 9, len(PAYLOAD)))
        self.fixture['board']['board_name'] = 'other'
        self.sync_fixture()
        before = len(self.calls())
        self.assertEqual(self.worker('prepare'), 1)
        self.assertFalse(any(row[0] == 'curl' for row in self.calls()[before:]))
        self.assertNotIn(['helper', 'flash'], self.calls())

    def test_failed_worker_launch_clears_lock_and_sets_error_phase(self):
        self.fixture['start_failure'] = 1
        self.sync_fixture()
        result = self.call('update_check')
        self.assertFalse(result['ok'])
        self.assertEqual(self.status()['phase'], 'error')
        self.assertFalse((self.root / 'lock').exists())

    def test_corrupt_download_and_changed_pinned_release_never_reach_ready(self):
        for failure in ('hash', 'oversize', 'changed release'):
            with self.subTest(failure=failure):
                if failure != 'hash':
                    self.tearDown()
                    self.setUp()
                self.call('update_check')
                self.assertEqual(self.worker('check'), 0)
                if failure == 'changed release':
                    self.manifest['images']['sysupgrade']['sha256'] = '0' * 64
                    self.sync_fixture()
                else:
                    url = self.metadata['assets'][1]['browser_download_url']
                    self.fixture['downloads'][url] = base64.b64encode(
                        b'x' * len(PAYLOAD) if failure == 'hash' else PAYLOAD + b'oversize',
                    ).decode()
                    self.fixture_path.write_text(json.dumps(self.fixture))
                self.call('update_prepare', {'version': VERSION})
                self.assertEqual(self.worker('prepare'), 1)
                self.assertEqual(self.status()['phase'], 'error')
                self.assertNotIn(['helper', 'backup'], self.calls())
                self.assertNotIn(['helper', 'flash'], self.calls())

    def test_raw_backup_size_limit_blocks_ready_and_backup_download(self):
        self.fixture['backup'] = base64.b64encode(b'x' * 262145).decode()
        self.sync_fixture()
        check = self.call('update_check')
        self.assertEqual(self.worker('check'), 0)
        self.call('update_prepare', {'version': VERSION})
        self.assertEqual(self.worker('prepare'), 1)
        self.assertEqual(self.status()['phase'], 'error')
        self.assertFalse(self.call('update_backup', {'job_id': check['job_id']})['ok'])

    def test_lock_collision_stale_job_and_cancelled_preparation_never_flash(self):
        self.status()
        (self.root / 'lock').mkdir()
        self.assertFalse(self.call('update_check')['ok'])
        (self.root / 'lock').rmdir()
        job = self.call('update_check')['job_id']
        path = self.root / 'state.json'
        state = json.loads(path.read_text())
        state['updated_at'] = int(time.time()) - 1300
        path.write_text(json.dumps(state))
        self.assertEqual(self.status()['phase'], 'error')
        self.assertFalse((self.root / 'lock').exists())
        self.assertFalse(self.call('update_apply', {'job_id': job, 'confirmation': '', 'backup_saved': False})['ok'])
        self.assertNotIn(['helper', 'flash'], self.calls())


@unittest.skipUnless(os.name == 'posix' and Path('/opt/wifi-proxy/runtime-check/tmp/qemu-mipsel-static').is_file(),
                     'Requires disposable MIPS QEMU runtime')
class MipsContractTests(unittest.TestCase):
    def test_actual_mips_modules_load_and_status_stays_inside_chroot(self):
        base = Path('/opt/wifi-proxy/runtime-check')
        with tempfile.TemporaryDirectory(prefix='web-update-mips-') as temp:
            root = Path(temp)
            for name in ('lib', 'usr/lib'):
                shutil.copytree(base / name, root / name, symlinks=True)
            for name in ('usr/bin/ucode', 'bin/busybox', 'tmp/qemu-mipsel-static'):
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(base / name, destination)
            for name in ('usr/libexec/rpcd/wifi_proxy', 'usr/share/wifi-proxy/update.uc',
                         'usr/share/wifi-proxy/status.uc', 'usr/share/wifi-proxy/profile.uc', 'usr/share/wifi-proxy/awg.uc'):
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((REPO / 'overlay' / name).read_bytes().replace(b'\r\n', b'\n'))
            prefix = ['chroot', str(root), '/tmp/qemu-mipsel-static', '/usr/bin/ucode']
            result = subprocess.run(prefix + ['/usr/libexec/rpcd/wifi_proxy', 'list'], text=True,
                                    capture_output=True, check=True, timeout=10)
            methods = json.loads(result.stdout)
            self.assertEqual(methods['update_apply'], {'job_id': '', 'confirmation': '', 'backup_saved': False})
            result = subprocess.run(prefix + ['/usr/libexec/rpcd/wifi_proxy', 'call', 'update_status'], input='{}',
                                    text=True, capture_output=True, check=True, timeout=10)
            self.assertEqual(json.loads(result.stdout)['phase'], 'idle')
            self.assertTrue((root / 'tmp/proxys22-update').is_dir())
            backup = root / 'tmp/proxys22-update/backup.tar.gz'
            backup.write_bytes(archive_bytes())
            (root / 'dev').mkdir()
            (root / 'dev/null').touch()
            busybox = ['chroot', str(root), '/tmp/qemu-mipsel-static', '/bin/busybox']
            subprocess.run(busybox + ['gzip', '-t', '/tmp/proxys22-update/backup.tar.gz'], check=True,
                           capture_output=True, timeout=10)
            result = subprocess.run(busybox + ['tar', '-tvzf', '/tmp/proxys22-update/backup.tar.gz'], check=True,
                                    text=True, capture_output=True, timeout=10)
            self.assertTrue(all(any(line.startswith('-') and line.endswith(name.lstrip('/'))
                                    for line in result.stdout.splitlines()) for name in REQUIRED))
            backup.write_bytes(backup.read_bytes()[:-8])
            self.assertNotEqual(subprocess.run(busybox + ['gzip', '-t', '/tmp/proxys22-update/backup.tar.gz'],
                                               capture_output=True, timeout=10).returncode, 0)


@unittest.skipUnless(os.name == 'posix', 'Requires Linux/WSL shell')
class FixedHelperTests(unittest.TestCase):
    def test_helper_uses_only_preserving_sysupgrade_actions_and_rejects_extra_modes(self):
        with tempfile.TemporaryDirectory(prefix='web-update-helper-') as temp:
            root = Path(temp)
            calls = root / 'calls.jsonl'
            stub = root / 'sysupgrade'
            stub.write_text(f'#!{sys.executable}\nimport json,sys\nwith open({str(calls)!r}, "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")\n')
            stub.chmod(0o755)
            source = (REPO / 'overlay/usr/libexec/wifi-proxy/update').read_text()
            source = source.replace('/tmp/proxys22-update', str(root)).replace('/sbin/sysupgrade', str(stub))
            source = source.replace('/bin/sleep 2', ':')
            for mode in ('backup', 'test-image', 'flash'):
                result = subprocess.run(['/bin/sh', '-s', '--', mode], input=source, text=True,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([json.loads(line) for line in calls.read_text().splitlines()], [
                ['-b', str(root / 'backup.tar.gz')], ['-T', str(root / 'image.bin')], [str(root / 'image.bin')],
            ])
            before = calls.read_bytes()
            result = subprocess.run(['/bin/sh', '-s', '--', 'flash; reboot'], input=source, text=True,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(calls.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
