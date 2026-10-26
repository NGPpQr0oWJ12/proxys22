"""Wi-Fi helper fallback uses isolated interfaces and command stubs only."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix', 'Run shell scan tests under Linux/WSL')
class WifiScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wifi-scan-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.interfaces = self.root / 'interfaces'
        self.tools = self.root / 'tools'
        self.interfaces.mkdir()
        self.tools.mkdir()
        self.responses = {}
        self.calls = self.root / 'calls.jsonl'
        self.mutations = self.root / 'mutations.log'
        self.config = self.root / 'wireless'
        self.config.write_text('fixture existing STA configuration\n')
        stub = self.tools / 'iwinfo'
        stub.write_text(f'#!{sys.executable}\n' + r'''
import json, os, pathlib, sys
with open(os.environ['TEST_SCAN_CALLS'], 'a') as output:
    output.write(json.dumps(sys.argv[1:]) + '\n')
responses = json.loads(os.environ['TEST_SCAN_RESPONSES'])
response = responses.get(sys.argv[1], {'code': 1, 'stdout': 'unexpected interface\n'})
sys.stdout.write(response.get('stdout', ''))
sys.stderr.write(response.get('stderr', 'fixture iwinfo diagnostic\n'))
sys.exit(response['code'])
''')
        stub.chmod(0o755)
        for name in ('wifi', 'uci', 'ip', 'iw', 'ifup', 'ifdown', 'reboot', 'service'):
            mutation = self.tools / name
            mutation.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$TEST_SCAN_MUTATIONS"\nexit 97\n')
            mutation.chmod(0o755)
        source = Path(__file__).resolve().parents[1] / 'overlay/usr/libexec/wifi-proxy/devices'
        self.source = source.read_text().replace(
            '/sys/class/net/*/phy80211', shlex.quote(str(self.interfaces)) + '/*/phy80211',
        ).replace('/usr/bin/iwinfo', shlex.quote(str(stub))).replace(
            'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
            'PATH=' + shlex.quote(str(self.tools)) + ':/usr/bin:/bin',
        )

    def interface(self, name, code, stdout=''):
        (self.interfaces / name / 'phy80211').mkdir(parents=True)
        self.responses[name] = {'code': code, 'stdout': stdout}

    def scan(self):
        env = {
            **os.environ,
            'TEST_SCAN_CALLS': str(self.calls),
            'TEST_SCAN_MUTATIONS': str(self.mutations),
            'TEST_SCAN_RESPONSES': json.dumps(self.responses),
            'LC_ALL': 'C',
        }
        return subprocess.run(
            ['/bin/sh', '-s', '--', 'scan'], input=self.source,
            text=True, capture_output=True, env=env, timeout=5,
        )

    def scanned_interfaces(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_failed_first_interface_tries_next_without_mixing_output(self):
        self.interface('a-ap', 1, 'partial failed scan output\n')
        self.interface('b-sta', 0, 'Cell 01 - Address: 02:00:00:00:00:01\n ESSID: "Nearby"\n')
        self.interface('c-spare', 0, 'must not be scanned\n')
        result = self.scan()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, self.responses['b-sta']['stdout'])
        self.assertEqual(result.stderr, '')
        self.assertEqual(self.scanned_interfaces(), [['a-ap', 'scan'], ['b-sta', 'scan']])

    def test_all_failed_interfaces_return_error_without_failed_output(self):
        self.interface('a-ap', 1, 'first failure\n')
        self.interface('b-sta', 2, 'second failure\n')
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((result.stdout, result.stderr), ('', ''))
        self.assertEqual(self.scanned_interfaces(), [['a-ap', 'scan'], ['b-sta', 'scan']])

    def test_successful_empty_scan_remains_success_and_stops_search(self):
        self.interface('a-ap', 0)
        self.interface('b-sta', 0, 'must not replace an empty successful scan\n')
        result = self.scan()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.scanned_interfaces(), [['a-ap', 'scan']])

    def test_no_interfaces_returns_error_without_running_iwinfo(self):
        result = self.scan()
        self.assertEqual(result.returncode, 1)
        self.assertEqual((result.stdout, result.stderr), ('', ''))
        self.assertEqual(self.scanned_interfaces(), [])

    def test_unsafe_interface_names_are_skipped(self):
        for name in ('a;command', 'a space', 'a$variable', 'a"quote'):
            self.interface(name, 0, 'must not run\n')
        self.interface('z_valid-0.1:2', 0, 'valid interface\n')
        result = self.scan()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'valid interface\n')
        self.assertEqual(self.scanned_interfaces(), [['z_valid-0.1:2', 'scan']])

    def test_scan_does_not_restart_radio_or_mutate_configuration(self):
        original = self.config.read_bytes()
        self.interface('a-ap', 1, 'failed\n')
        self.interface('b-sta', 0, 'successful\n')
        self.assertEqual(self.scan().returncode, 0)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.mutations.exists())
        self.assertTrue(all(arguments[1:] == ['scan'] for arguments in self.scanned_interfaces()))


if __name__ == '__main__':
    unittest.main()
