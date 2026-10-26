"""Exercise the actual POSIX workers with isolated paths and mocked kernel/network tools."""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
MOCK = r'''import json, os, pathlib, sys, time
root = pathlib.Path(os.environ['FAILOVER_TEST_ROOT'])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
state_path = root / 'kernel.json'
state = json.loads(state_path.read_text())
payload = sys.stdin.read() if name == 'nft' and args == ['-f', '-'] else ''
with (root / 'events.jsonl').open('a') as out:
    out.write(json.dumps({'tool': name, 'args': args, 'input': payload}) + '\n')
if name == 'uci':
    print((root / ('protocol' if args[-1].endswith('.protocol') else 'enabled')).read_text().strip())
elif name == 'curl':
    setting = json.loads((root / 'probe.json').read_text())
    if setting.get('disable'):
        (root / 'enabled').write_text('0')
    if setting.get('restart'):
        (root / 'run/client.generation').write_text('new-client')
    backup = args[-1].endswith('generate_204')
    value = setting.get('backup' if backup else 'primary', 'ok')
    if value == 'fail':
        sys.exit(7)
    print(('204' if backup else 'Microsoft Connect Test') if value == 'ok' else value, end='')
elif name == 'nft':
    if args == ['list', 'table', 'ip', 'wifi_proxy']:
        sys.exit(0 if state.get('table') else 1)
    elif args == ['delete', 'table', 'ip', 'wifi_proxy']:
        state['table'] = False
    elif args == ['-f', '-']:
        if (root / 'fail-nft').exists():
            sys.exit(1)
        if not state.get('route') or not state.get('rule'):
            sys.exit('capture installed before policy routing')
        state['table'] = True
        (root / 'nft.batch').write_text(payload)
    else:
        sys.exit('unexpected nft invocation')
elif name == 'ip':
    if args == ['link', 'show', 'awg0']:
        sys.exit(0 if state.get('awg_link', True) else 1)
    elif args == ['-4', 'rule', 'show']:
        if state.get('rule'):
            print('100: from all fwmark 0x100/0x100 lookup 100')
        if state.get('awg_rule'):
            print('101: from all iif br-lan lookup 201')
    elif args[:3] == ['-4', 'rule', 'add']:
        state['awg_rule' if args[-1] == '201' else 'rule'] = True
    elif args[:3] == ['-4', 'rule', 'del']:
        if state.get('table'):
            sys.exit('policy rule removed before capture')
        state['awg_rule' if args[-1] == '201' else 'rule'] = False
    elif args[:3] == ['-4', 'route', 'replace']:
        state['route'] = True
    elif args == ['-4', 'route', 'flush', 'table', '100']:
        if state.get('table'):
            sys.exit('policy route removed before capture')
        state['route'] = False
    else:
        sys.exit('unexpected ip invocation')
elif name == 'sleep':
    time.sleep(0.03)
elif name not in ('killall', 'logger'):
    sys.exit('unexpected mock tool')
state_path.write_text(json.dumps(state))
'''


@unittest.skipUnless(os.name == 'posix', 'Run these shell tests under Linux/WSL')
class FailoverHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wifi-proxy-health-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'run'
        self.runtime.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.profile = self.root / 'client.json'
        self.profile.write_text('{}')
        (self.runtime / 'client.generation').write_text('first-client')
        (self.root / 'enabled').write_text('1')
        (self.root / 'protocol').write_text('hysteria')
        (self.root / 'awg.json').write_text('{}')
        (self.root / 'probe.json').write_text('{}')
        (self.root / 'kernel.json').write_text('{}')
        self.env = {**os.environ, 'PATH': str(self.bin), 'FAILOVER_TEST_ROOT': str(self.root)}
        # No real curl, ip, nft or killall is ever reachable in this PATH.
        for tool in ('cat', 'chmod', 'grep', 'mkdir', 'mv', 'rm', 'rmdir'):
            (self.bin / tool).symlink_to(shutil.which(tool))
        for tool in ('uci', 'curl', 'nft', 'ip', 'killall', 'logger', 'sleep'):
            path = self.bin / tool
            path.write_text(f'#!{sys.executable}\n' + MOCK)
            path.chmod(0o755)
        self.routes = self.root / 'routes'
        self.health = self.root / 'health'
        for source, destination in (
            ('overlay/usr/libexec/wifi-proxy/routes', self.routes),
            ('overlay/usr/libexec/wifi-proxy/health', self.health),
        ):
            text = (REPO / source).read_text().replace('/var/run/wifi-proxy', str(self.runtime))
            text = text.replace('/etc/wifi-proxy/client.json', str(self.profile))
            text = text.replace('/etc/wifi-proxy/awg.json', str(self.root / 'awg.json'))
            text = text.replace('/usr/libexec/wifi-proxy/routes', str(self.routes))
            destination.write_text(text)
            destination.chmod(0o755)

    def run_script(self, path, *args, check=True):
        return subprocess.run(['/bin/sh', str(path), *args], env=self.env, capture_output=True,
                              text=True, timeout=10, check=check)

    def tick(self, **probe):
        (self.root / 'probe.json').write_text(json.dumps(probe))
        self.run_script(self.health, 'once')
        return self.state()

    def state(self):
        return json.loads((self.runtime / 'route.json').read_text())

    def events(self, tool=None):
        events = [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines()]
        return [event for event in events if tool is None or event['tool'] == tool]

    def assert_direct(self, reason):
        self.assertEqual(self.state(), {'mode': 'direct', 'healthy': False, 'reason': reason})
        self.assertEqual((self.runtime / 'dns.servers').read_text(),
                         'server=9.9.9.9\nserver=1.1.1.1\n')
        kernel = json.loads((self.root / 'kernel.json').read_text())
        self.assertFalse(kernel.get('table'))
        self.assertFalse(kernel.get('rule'))
        self.assertFalse(kernel.get('route'))

    def test_startup_outage_and_two_success_recovery(self):
        self.run_script(self.routes, 'start')
        self.assert_direct('starting')
        inode = (self.runtime / 'dns.servers').stat().st_ino
        self.assertEqual(self.tick()['reason'], 'recovering')
        self.assertEqual(self.tick()['mode'], 'proxy')
        self.assertEqual((self.runtime / 'dns.servers').read_text(), 'server=127.0.0.1#5353\n')
        self.assertEqual(self.tick(primary='fail', backup='fail')['mode'], 'direct')
        self.assert_direct('probe_failed')
        self.assertEqual(self.tick()['reason'], 'recovering')
        self.assertEqual(self.tick()['mode'], 'proxy')
        self.assertEqual((self.runtime / 'dns.servers').stat().st_ino, inode)
        self.assertEqual((self.runtime / 'dns.servers').stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.runtime / 'route.json').stat().st_mode & 0o777, 0o600)

    def test_awg_failure_and_recovery_keep_probe_routing_separate(self):
        (self.root / 'protocol').write_text('amneziawg')
        self.assertEqual(self.tick()['reason'], 'recovering')
        self.assertEqual(self.tick()['mode'], 'proxy')
        self.assertEqual((self.runtime / 'dns.servers').read_text(), 'server=1.1.1.1@awg0\n')
        kernel = json.loads((self.root / 'kernel.json').read_text())
        self.assertTrue(kernel['awg_rule'])
        self.assertFalse(kernel.get('table'))
        for event in self.events('curl'):
            self.assertIn('--interface', event['args'])
            self.assertIn('awg0', event['args'])
            self.assertNotIn('--socks5-hostname', event['args'])
        self.tick(primary='fail', backup='fail')
        self.assert_direct('probe_failed')
        kernel = json.loads((self.root / 'kernel.json').read_text())
        self.assertFalse(kernel['awg_rule'])
        self.assertFalse(any(e['args'][-2:] == ['table', '201'] for e in self.events('ip')))
        self.assertEqual(self.tick()['reason'], 'recovering')
        self.assertEqual(self.tick()['mode'], 'proxy')

    def test_awg_missing_link_keeps_lan_direct(self):
        (self.root / 'protocol').write_text('amneziawg')
        (self.root / 'kernel.json').write_text('{"awg_link": false}')
        self.tick()
        self.tick()
        self.assert_direct('route_error')

    def test_awg_missing_profile_does_not_reuse_hysteria(self):
        (self.root / 'protocol').write_text('amneziawg')
        (self.root / 'awg.json').unlink()
        self.tick()
        self.assert_direct('unconfigured')

    def test_recovery_must_be_consecutive_and_probe_content_must_match(self):
        self.tick()
        self.tick(primary='captive portal', backup='200')
        self.assert_direct('probe_failed')
        self.assertEqual(self.tick()['reason'], 'recovering')
        self.assertEqual(self.tick()['mode'], 'proxy')

    def test_backup_endpoint_prevents_false_outage(self):
        self.tick(primary='fail')
        self.assertEqual(self.tick(primary='wrong body')['mode'], 'proxy')
        calls = self.events('curl')
        self.assertEqual(len(calls), 4)
        for call in calls:
            args = call['args']
            self.assertEqual(args[args.index('--socks5-hostname') + 1], '127.0.0.1:1080')
            self.assertEqual(args[args.index('--noproxy') + 1], '')
            self.assertEqual(args[args.index('--max-time') + 1], '4')
            self.assertNotIn('--location', args)

    def test_disabled_unconfigured_stopped_and_missing_dependency_use_direct(self):
        (self.root / 'enabled').write_text('0')
        self.tick()
        self.assert_direct('disabled')
        self.assertFalse(self.events('curl'))
        (self.root / 'enabled').write_text('1')
        self.profile.unlink()
        self.tick()
        self.assert_direct('unconfigured')
        self.profile.write_text('{}')
        (self.runtime / 'client.generation').unlink()
        self.tick()
        self.assert_direct('client_stopped')
        (self.runtime / 'client.generation').write_text('next')
        (self.bin / 'curl').unlink()
        self.tick()
        self.assert_direct('dependency_missing')

    def test_disable_or_restart_during_probe_cannot_restore_proxy(self):
        self.tick()
        self.tick(disable=True)
        self.assert_direct('disabled')
        (self.root / 'enabled').write_text('1')
        self.tick()
        self.tick(restart=True)
        self.assert_direct('client_restarting')

    def test_atomic_capture_batch_and_policy_routing_order(self):
        self.run_script(self.routes, 'proxy', 'healthy')
        batch = (self.root / 'nft.batch').read_text()
        self.assertEqual(batch.count('add table ip wifi_proxy\n'), 1)
        self.assertIn('flush table ip wifi_proxy\n', batch)
        self.assertIn('meta l4proto tcp tproxy', batch)
        self.assertIn('meta l4proto udp tproxy', batch)
        self.assertNotIn('proxys22_devices', batch)
        addresses = batch.split('ip daddr { ', 1)[1].split(' }', 1)[0].split(', ')
        for address in addresses:
            ipaddress.ip_network(address)
        self.run_script(self.routes, 'stop')
        self.assert_direct('stopped')
        self.assertEqual(len(self.events('killall')), 2)

    def test_nft_failure_removes_old_capture_and_restores_direct_dns(self):
        self.run_script(self.routes, 'proxy', 'healthy')
        (self.root / 'fail-nft').touch()
        result = self.run_script(self.routes, 'proxy', 'healthy', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assert_direct('route_error')

    def test_successful_polls_do_not_rewrite_rules_or_signal_dnsmasq(self):
        self.tick()
        self.tick()
        before = len(self.events('killall'))
        self.tick()
        self.assertEqual(len(self.events('killall')), before)
        self.assertEqual(sum(event['args'] == ['-f', '-'] for event in self.events('nft')), 1)
        kernel = json.loads((self.root / 'kernel.json').read_text())
        kernel['table'] = False
        (self.root / 'kernel.json').write_text(json.dumps(kernel))
        self.tick()
        self.assertEqual(sum(event['args'] == ['-f', '-'] for event in self.events('nft')), 2)

    def test_direct_polls_do_not_rewrite_policy_routing(self):
        (self.root / 'enabled').write_text('0')
        self.tick()
        before = len(self.events('ip'))
        self.tick()
        self.tick()
        self.assertEqual(len(self.events('ip')), before)

    def test_empty_or_dead_lock_is_recovered(self):
        lock = self.runtime / 'routes.lock'
        lock.mkdir()
        self.run_script(self.routes, 'direct', 'starting')
        self.assert_direct('starting')
        self.assertFalse(lock.exists())
        lock.mkdir()
        (lock / 'pid').write_text('999999999')
        self.run_script(self.routes, 'direct', 'disabled')
        self.assert_direct('disabled')
        self.assertFalse(lock.exists())

    def test_route_lock_rechecks_disabled_stopped_and_restarted_client(self):
        self.run_script(self.routes, 'proxy', 'healthy', 'old-client')
        self.assert_direct('client_restarting')
        (self.runtime / 'client.generation').unlink()
        self.run_script(self.routes, 'proxy', 'healthy')
        self.assert_direct('client_stopped')
        (self.runtime / 'client.generation').write_text('new-client')
        (self.root / 'enabled').write_text('0')
        self.run_script(self.routes, 'proxy', 'healthy')
        self.assert_direct('disabled')

    def test_worker_shutdown_restores_direct(self):
        process = subprocess.Popen(['/bin/sh', str(self.health), 'run'], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if (self.runtime / 'route.json').exists() and self.state()['mode'] == 'proxy':
                    break
                time.sleep(0.03)
            else:
                self.fail('health worker did not reach proxy mode')
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, error)
            self.assert_direct('stopped')
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_shell_syntax(self):
        for path in ('overlay/etc/init.d/hysteria', 'overlay/etc/init.d/wifi-proxy-health',
                     'overlay/usr/libexec/wifi-proxy/routes', 'overlay/usr/libexec/wifi-proxy/health'):
            subprocess.run(['/bin/sh', '-n', str(REPO / path)], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
