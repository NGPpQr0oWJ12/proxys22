"""Run real MIPS rpcd/uci code in an isolated copy; network helper is stubbed."""
import json
from pathlib import Path
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
assert root == Path('/opt/wifi-proxy/runtime-check')
assert (root / 'tmp/qemu-mipsel-static').is_file()
command = ['chroot', str(root), '/tmp/qemu-mipsel-static', '/usr/bin/ucode', '/usr/libexec/rpcd/wifi_proxy']


def call(method, payload):
    result = subprocess.run(command + ['call', method], input=json.dumps(payload), text=True,
                            capture_output=True, timeout=10, check=True)
    return json.loads(result.stdout)


methods = json.loads(subprocess.check_output(command + ['list'], text=True, timeout=10))
assert set(methods) == {'status', 'import_profile', 'import_awg', 'set_enabled', 'set_uplink', 'set_wifi', 'scan_wifi', 'set_device_blocked', 'update_check', 'update_status', 'update_prepare', 'update_backup', 'update_apply'}
config_dir = root / 'etc/config'
config_dir.mkdir(exist_ok=True)
(config_dir / 'wireless').write_text("config wifi-device 'radio0'\n\toption disabled '1'\nconfig wifi-iface 'default_radio0'\n\toption device 'radio0'\n\toption network 'lan'\n\toption mode 'ap'\n")
(config_dir / 'network').write_text("config interface 'lan'\n\toption device 'br-lan'\n\toption ipaddr '192.168.2.1'\nconfig interface 'wan'\n\toption device 'eth0.2'\n\toption proto 'dhcp'\n")
(root / 'etc/wifi-proxy').mkdir(exist_ok=True)

assert call('import_profile', {'uri': 'bad'})['ok'] is False
assert call('set_enabled', {'enabled': 'true'})['ok'] is False
assert call('set_enabled', {'enabled': True})['ok'] is False
assert call('import_profile', {'uri': 'hy2://synthetic-test@gateway.example?sni=tls.example'})['ok'] is True
profile_path = root / 'etc/wifi-proxy/client.json'
profile = json.loads(profile_path.read_text())
assert profile_path.stat().st_mode & 0o777 == 0o600
assert profile['auth'] == 'synthetic-test' and profile['tls']['insecure'] is False
assert profile['lazy'] is True
assert call('set_enabled', {'enabled': True})['ok'] is True
state = call('status', {})
assert state['tunnel']['enabled'] and state['tunnel']['configured']
assert 'auth' not in json.dumps(state) and 'synthetic-test' not in json.dumps(state)
awg = '''[Interface]
PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
Address = 10.77.22.2/32
Jc = 3
Jmin = 20
Jmax = 80
S1 = 23
S2 = 41
H1 = 123456
H2 = 123457
H3 = 123458
H4 = 123459
[Peer]
PublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
AllowedIPs = 0.0.0.0/0
Endpoint = gateway.example:54433
'''
assert call('import_awg', {'config': awg})['ok'] is True
awg_path = root / 'etc/wifi-proxy/awg.json'
assert awg_path.stat().st_mode & 0o777 == 0o600
state = call('status', {})
assert state['tunnel']['protocol'] == 'amneziawg' and state['tunnel']['configured']
assert 'PrivateKey' not in json.dumps(state) and 'AAAAAA' not in json.dumps(state)
saved_awg = awg_path.read_text()
assert call('import_awg', {'config': awg.replace('Jc = 3', 'PostUp = command')})['ok'] is False
assert awg_path.read_text() == saved_awg
assert profile_path.read_text() and json.loads(profile_path.read_text()) == profile
# The saved Hysteria client can be selected again without deleting the AWG profile.
assert call('import_profile', {'uri': 'hy2://synthetic-test@gateway.example?sni=tls.example'})['ok'] is True
assert call('status', {})['tunnel']['protocol'] == 'hysteria'
assert awg_path.read_text() == saved_awg
assert call('set_wifi', {'ssid': 'Test network', 'password': 'sample-only-pass'})['ok'] is True
assert call('set_wifi', {'ssid': 'Test', 'password': 'short'})['ok'] is False
assert call('set_uplink', {'mode': 'wifi', 'ssid': "sample';not-a-command", 'password': 'test-only-pass'})['ok'] is True
assert call('status', {})['uplink']['mode'] == 'wifi'
network = (config_dir / 'network').read_text()
assert "br-lan" in network and '192.168.2.1' in network and 'eth0.2' in network
assert call('set_uplink', {'mode': 'ethernet'})['ok'] is True
assert call('set_enabled', {'enabled': False})['ok'] is True
assert not call('status', {})['tunnel']['enabled']
print('PASS: MIPS rpcd schema, UCI operations, private profile storage and input validation')
