"""Older sysupgrade.conf must not hide the new private AWG profile keep rule."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'posix', 'Run migration shell tests under Linux/WSL')
class AwgUpgradeTests(unittest.TestCase):
    def test_old_keep_list_is_extended_and_migration_is_idempotent(self):
        source = Path(__file__).resolve().parents[1] / 'overlay/etc/uci-defaults/93-proxys22-awg'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            keep = root / 'sysupgrade.conf'
            original = '/etc/wifi-proxy/client.json\n/etc/wifi-proxy/initialized\n/custom-keep-file\n'
            keep.write_text(original)
            stub = root / 'uci'
            stub.write_text('#!/bin/sh\ncat >/dev/null\n')
            stub.chmod(0o755)
            text = source.read_text().replace('/etc/sysupgrade.conf', str(keep))
            text = text.replace('/etc/init.d/amneziawg enable', ':')
            env = {**os.environ, 'PATH': str(root) + ':' + os.environ['PATH']}
            for _ in range(2):
                subprocess.run(['/bin/sh'], input=text, text=True, env=env, check=True, timeout=5)
            self.assertTrue(keep.read_text().startswith(original))
            self.assertEqual(keep.read_text().splitlines().count('/etc/wifi-proxy/awg.json'), 1)
