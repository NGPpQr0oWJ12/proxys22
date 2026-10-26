"""Isolated MIPS rpcd/UCI integration. All commands are local native test stubs."""
import json
from pathlib import Path
import shutil
import subprocess

repo = Path(__file__).resolve().parents[1]
base = Path('/opt/wifi-proxy/runtime-check')
root = Path('/opt/wifi-proxy/devices-check')
assert base.is_dir() and root == Path('/opt/wifi-proxy/devices-check')
# Reset only this exact disposable fixture; never follow a substituted root/parent.
assert root.is_absolute() and not root.is_symlink()
assert root.resolve() == Path('/opt/wifi-proxy/devices-check')
assert root.parent.resolve() == Path('/opt/wifi-proxy') and root != base.resolve()
if root.exists():
    assert root.is_dir()
    shutil.rmtree(root)
shutil.copytree(base, root, symlinks=True,
                ignore=shutil.ignore_patterns('proc', 'sys', 'dev'))
for name in ['var', 'proc', 'sys', 'dev']:
    target = root / name
    if target.is_symlink():
        target.unlink()
    target.mkdir(exist_ok=True)
for name in ['var/run/wifi-proxy', 'etc/config', 'tmp', 'sys/class/net/br-lan/statistics']:
    (root / name).mkdir(parents=True, exist_ok=True)
for name in ['usr/libexec/rpcd/wifi_proxy', 'usr/share/wifi-proxy/status.uc',
             'usr/share/wifi-proxy/profile.uc', 'usr/libexec/wifi-proxy/devices']:
    destination = root / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((repo / 'overlay' / name).read_bytes().replace(b'\r\n', b'\n'))
    destination.chmod(0o755 if 'libexec' in name else 0o644)

mock = root / 'tmp/mock-shell.c'
mock.write_text(r'''
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <stdlib.h>
int main(int argc, char **argv) {
  if (argc >= 3 && !strcmp(argv[1], "-c")) {
    if (!strcmp(argv[2], "/usr/libexec/wifi-proxy/devices nft")) {
      if (!access("/tmp/nft-fail", F_OK)) return 1;
      FILE *f = fopen("/tmp/nft-rules", "w"); if (!f) return 1;
      int c; while ((c = getchar()) != EOF) fputc(c, f);
      return fclose(f) ? 1 : 0;
    }
    if (!strcmp(argv[2], "/usr/libexec/wifi-proxy/devices neighbors")) {
      puts("[{\"dev\":\"br-lan\",\"lladdr\":\"02:00:00:00:00:11\",\"dst\":\"192.0.2.11\",\"state\":[\"REACHABLE\"]}]"); return 0;
    }
    if (!strcmp(argv[2], "/usr/libexec/wifi-proxy/devices scan")) {
      puts("Cell 01 - Address: 02:00:00:00:00:01\n ESSID: \"Test network\"\n Signal: -55 dBm\n Encryption: WPA PSK (CCMP)"); return 0;
    }
    return 1;
  }
  if (argc >= 3 && !strcmp(argv[1], "/usr/libexec/wifi-proxy/devices")) {
    if (!strcmp(argv[2], "check")) return access("/tmp/nft-rules", F_OK) ? 1 : 0;
    if (!strcmp(argv[2], "apply") || !strcmp(argv[2], "set")) {
      char *args[] = {"/tmp/qemu-mipsel-static", "/usr/bin/ucode", "-e",
        "import { device_command } from '/usr/share/wifi-proxy/status.uc'; exit(device_command(ARGV));",
        "--", argv[2], argc > 3 ? argv[3] : NULL, argc > 4 ? argv[4] : NULL, NULL};
      execv(args[0], args); return 1;
    }
  }
  return 1;
}
''')
shell = root / 'bin/sh'
if shell.is_symlink():
    shell.unlink()
subprocess.run(['gcc', '-static', '-Os', '-o', str(shell), str(mock)], check=True,
               capture_output=True, timeout=30)
(root / 'etc/config/wifi_proxy').write_text("config main 'main'\n option enabled '0'\n option uplink 'ethernet'\n")
(root / 'etc/config/network').write_text("config interface 'lan'\n option ipaddr '192.168.2.1'\n option netmask '255.255.255.0'\nconfig interface 'wan'\n option proto 'dhcp'\n")
(root / 'etc/config/wireless').write_text("config wifi-device 'radio0'\nconfig wifi-iface 'default_radio0'\n option ssid 'Test Wi-Fi'\n")
(root / 'etc/proxys22-release').write_text(json.dumps({'name': 'proxys22', 'version': '0.2.0'}))
(root / 'proc/stat').write_text('cpu 100 0 100 800 0 0 0 0\n')
(root / 'proc/uptime').write_text('10.00 0.00\n')
(root / 'sys/class/net/br-lan/statistics/rx_bytes').write_text('1000\n')
(root / 'sys/class/net/br-lan/statistics/tx_bytes').write_text('2000\n')
(root / 'tmp/dhcp.leases').write_text('0 02:00:00:00:00:11 192.0.2.11 test-device *\n')
for name in ['tmp/nft-rules', 'tmp/nft-fail', 'var/run/wifi-proxy/metrics.json', 'var/run/wifi-proxy/route.json', 'var/run/wifi-proxy/devices-ready.json']:
    (root / name).unlink(missing_ok=True)

command = ['chroot', str(root), '/tmp/qemu-mipsel-static', '/usr/bin/ucode', '/usr/libexec/rpcd/wifi_proxy']
def call(method, payload):
    result = subprocess.run(command + ['call', method], input=json.dumps(payload),
                            text=True, capture_output=True, check=True, timeout=15)
    return json.loads(result.stdout)

assert call('set_device_blocked', {'mac': '02:00:00:00:00:22;reboot', 'blocked': True})['ok'] is False
assert call('set_device_blocked', {'mac': '02:00:00:00:00:22', 'blocked': 'true'})['ok'] is False
assert not (root / 'tmp/nft-rules').exists()
assert call('set_device_blocked', {'mac': '02:AA:00:00:00:22', 'blocked': True})['ok'] is True
persisted = (root / 'etc/config/wifi_proxy').read_text()
assert "list blocked '02:aa:00:00:00:22'" in persisted
state = call('status', {})
assert state['system']['version'] == '0.2.0' and state['system']['firmware'] == 'proxys22 0.2.0'
assert state['capabilities']['set_device_blocked'] is True
assert any(row['mac'] == '02:aa:00:00:00:22' and row['blocked'] for row in state['devices'])
assert not ('mode' in state['tunnel'] or 'healthy' in state['tunnel'])
assert call('set_device_blocked', {'mac': '02:AA:00:00:00:22', 'blocked': True})['ok'] is True
assert (root / 'etc/config/wifi_proxy').read_text().count("list blocked") == 1
(root / 'tmp/nft-fail').touch()
assert call('set_device_blocked', {'mac': '02:AA:00:00:00:22', 'blocked': False})['ok'] is False
assert (root / 'etc/config/wifi_proxy').read_text() == persisted
assert call('status', {})['capabilities']['set_device_blocked'] is False
(root / 'tmp/nft-fail').unlink()
assert call('set_device_blocked', {'mac': '02:AA:00:00:00:22', 'blocked': False})['ok'] is True
assert 'list blocked' not in (root / 'etc/config/wifi_proxy').read_text()
before_scan = (root / 'etc/config/wireless').read_text()
assert call('scan_wifi', {})['networks'] == [{'ssid': 'Test network', 'signal': -55, 'security': 'psk-mixed'}]
assert (root / 'etc/config/wireless').read_text() == before_scan
assert call('set_uplink', {'mode': 'wifi', 'ssid': 'Legacy test', 'password': 'test-only-password', 'security': 'psk-mixed'})['ok'] is True
assert "option encryption 'psk-mixed'" in (root / 'etc/config/wireless').read_text()
assert call('set_uplink', {'mode': 'wifi', 'ssid': 'WPA2 test', 'password': 'test-only-password', 'security': 'psk2'})['ok'] is True
assert "option encryption 'psk2'" in (root / 'etc/config/wireless').read_text()
assert call('set_uplink', {'mode': 'wifi', 'ssid': 'Guest', 'password': '', 'security': 'open'})['ok'] is True
wireless = (root / 'etc/config/wireless').read_text()
assert "option encryption 'none'" in wireless and 'option key' not in wireless
assert call('set_uplink', {'mode': 'wifi', 'ssid': 'Unsupported', 'password': 'test-only-password', 'security': 'sae'})['ok'] is False
(root / 'proc/stat').write_text('cpu 250 0 250 1000 0 0 0 0\n')
(root / 'proc/uptime').write_text('15.00 0.00\n')
(root / 'sys/class/net/br-lan/statistics/rx_bytes').write_text('1500\n')
(root / 'sys/class/net/br-lan/statistics/tx_bytes').write_text('4500\n')
(root / 'var/run/wifi-proxy/route.json').write_text(json.dumps({'mode': 'direct', 'healthy': False, 'reason': 'unavailable', 'checked_at': 123}))
state = call('status', {})
assert state['metrics']['tx_bps'] == 800 and state['metrics']['rx_bps'] == 4000
assert state['metrics']['cpu_percent'] == 60
assert state['tunnel']['mode'] == 'direct' and state['tunnel']['healthy'] is False
assert 'password' not in json.dumps(state) and 'test-only-password' not in json.dumps(state)
print('PASS: isolated MIPS rpcd, MAC/bool validation, block/unblock UCI persistence, nft-failure rollback, readiness, metrics, open/WPA2 scan and truthful route/version status')
