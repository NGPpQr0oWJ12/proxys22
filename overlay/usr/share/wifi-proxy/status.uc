import { readfile, open, rename, unlink, mkdir, popen, access } from 'fs';

const runtime = '/var/run/wifi-proxy';
function read_json(path) {
	try { return json(readfile(path, 131072) || '{}'); } catch (e) { return {}; }
}
function save_json(path, value) {
	mkdir(runtime, 0o700);
	for (let n = 0; n < 16; n++) {
		let temporary = path + '.new.' + n, file = open(temporary, 'wx', 0o600);
		if (!file) continue;
		let body = sprintf('%J\n', value), written = file.write(body), closed = file.close();
		if (written == length(body) && closed && rename(temporary, path)) return true;
		unlink(temporary);
	}
	return false;
}
function canonical_mac(value) {
	if (type(value) != 'string' || !match(value, /^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$/)) return null;
	value = lc(value);
	if ((int(substr(value, 0, 2), 16) & 1) || value == '00:00:00:00:00:00') return null;
	return value;
}
function blocked_list(uci) {
	let values = uci.get('wifi_proxy', 'main', 'blocked'), result = [];
	if (type(values) != 'array') values = type(values) == 'string' ? [values] : [];
	for (let value in values) {
		let mac = canonical_mac(value);
		if (mac && index(result, mac) < 0 && length(result) < 256) push(result, mac);
	}
	return sort(result);
}
function metrics_delta(previous, current) {
	let result = { sampled_at: current.sampled_at };
	let elapsed = current.monotonic - previous.monotonic;
	if (type(previous.monotonic) != 'double' && type(previous.monotonic) != 'int') return result;
	if (elapsed <= 0 || elapsed > 300) return result;
	if (current.total > previous.total && current.idle >= previous.idle) {
		let usage = 100.0 - 100.0 * (current.idle - previous.idle) / (current.total - previous.total);
		result.cpu_percent = usage < 0 ? 0 : usage > 100 ? 100 : usage;
	}
	// br-lan receives client uploads and transmits client downloads. API is bits/s.
	if (current.rx != null && previous.rx != null && current.rx >= previous.rx)
		result.tx_bps = 8 * (current.rx - previous.rx) / elapsed;
	if (current.tx != null && previous.tx != null && current.tx >= previous.tx)
		result.rx_bps = 8 * (current.tx - previous.tx) / elapsed;
	return result;
}
function sample_metrics() {
	let fields = match(readfile('/proc/stat', 4096) || '', /^cpu\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)/);
	let up = split(readfile('/proc/uptime', 128) || '0', ' '), previous = read_json(runtime + '/metrics.json');
	let current = { monotonic: +up[0], sampled_at: time(), total: 0, idle: 0 };
	if (fields) {
		for (let i = 1; i <= 8; i++) current.total += +fields[i];
		current.idle = +fields[4] + +fields[5];
	}
	let rx = readfile('/sys/class/net/br-lan/statistics/rx_bytes', 64), tx = readfile('/sys/class/net/br-lan/statistics/tx_bytes', 64);
	current.rx = rx == null ? null : +trim(rx); current.tx = tx == null ? null : +trim(tx);
	if (previous.monotonic != null && current.monotonic - previous.monotonic >= 0 && current.monotonic - previous.monotonic < 1)
		return previous.metrics || { sampled_at: current.sampled_at };
	current.metrics = metrics_delta(previous, current);
	save_json(runtime + '/metrics.json', current);
	return current.metrics;
}
function fixed_read(command, maximum) {
	// Each caller supplies one constant helper command; never concatenate request data.
	let process = popen(command, 'r');
	if (!process) return null;
	let output = process.read(maximum), code = process.close();
	return code == 0 ? output : null;
}
function normalize_scan(rows) {
	let result = [], seen = {};
	for (let entry in rows || []) {
		if (type(entry.ssid) != 'string' || !length(entry.ssid) || length(entry.ssid) > 32 || index(entry.ssid, chr(0)) >= 0 || index(entry.ssid, '\r') >= 0 || index(entry.ssid, '\n') >= 0) continue;
		let security = entry.security;
		if (!security && entry.encryption?.enabled === false) security = 'open';
		if (!security && entry.encryption?.enabled && index(entry.encryption.authentication || [], 'psk') >= 0) {
			if (index(entry.encryption.wpa || [], 1) >= 0) security = 'psk-mixed';
			else if (index(entry.encryption.wpa || [], 2) >= 0) security = 'psk2';
		}
		if (security != 'open' && security != 'psk2' && security != 'psk-mixed') continue;
		let key = entry.ssid + '\n' + security;
		if (seen[key]) continue;
		seen[key] = true;
		push(result, { ssid: entry.ssid, signal: type(entry.signal) == 'int' ? entry.signal : null, security });
		if (length(result) >= 100) break;
	}
	return result;
}
function parse_iwinfo_scan(text) {
	let rows = [];
	for (let cell in split(text || '', /Cell \d+ - Address:/)) {
		let ssid = match(cell, /ESSID: "([^\r\n]*)"/), signal = match(cell, /Signal: (-?\d+) dBm/);
		let encryption = match(cell, /Encryption: ([^\r\n]+)/);
		if (!ssid || !encryption) continue;
		let value = lc(encryption[1]);
		if (index(value, 'none') >= 0) push(rows, { ssid: ssid[1], signal: signal ? +signal[1] : null, security: 'open' });
		else if (index(value, 'psk') >= 0) {
			// iwinfo prints WPA (without a digit) for WPA1, including mixed WPA/WPA2.
			let legacy = match(value, /(^|[^a-z0-9])wpa([\/ ]|$)/);
			if (legacy || index(value, 'wpa2') >= 0) push(rows, { ssid: ssid[1], signal: signal ? +signal[1] : null, security: legacy ? 'psk-mixed' : 'psk2' });
		}
	}
	return normalize_scan(rows);
}
function scan_wifi(bus) {
	if (bus) {
		let devices = bus.call('iwinfo', 'devices', {});
		for (let device in devices?.devices || []) {
			let scan = bus.call('iwinfo', 'scan', { device });
			if (type(scan?.results) == 'array') return normalize_scan(scan.results);
		}
	}
	let text = fixed_read('/usr/libexec/wifi-proxy/devices scan', 65536);
	return text == null ? null : parse_iwinfo_scan(text);
}
function merge_devices(leases, neighbors, associations, blocked, now) {
	let devices = {};
	function get(value) {
		let mac = canonical_mac(value);
		if (!mac) return null;
		if (!devices[mac]) devices[mac] = { mac, name: '', ip: '', connection: 'ethernet', connected: false, blocked: false };
		return devices[mac];
	}
	for (let line in split(leases || '', '\n')) {
		let fields = split(trim(line), /\s+/);
		if (length(fields) < 4 || (+fields[0] != 0 && +fields[0] < now)) continue;
		let device = get(fields[1]);
		if (!device) continue;
		device.ip = fields[2]; device.name = fields[3] == '*' ? '' : substr(fields[3], 0, 128);
	}
	for (let neighbor in neighbors || []) {
		if (neighbor.dev != 'br-lan') continue;
		let device = get(neighbor.lladdr);
		if (!device) continue;
		if (type(neighbor.dst) == 'string' && (!device.ip || index(neighbor.dst, ':') < 0)) device.ip = neighbor.dst;
		if (index(neighbor.state || [], 'REACHABLE') >= 0 || index(neighbor.state || [], 'PERMANENT') >= 0) device.connected = true;
	}
	for (let mac, association in associations || {}) {
		let device = get(mac);
		if (!device) continue;
		device.connection = 'wifi'; device.connected = !!association.assoc || !!association.authorized;
	}
	for (let mac in blocked || []) { let device = get(mac); if (device) device.blocked = true; }
	return values(devices);
}
function list_devices(bus, uci) {
	let neighbors = [], associations = {};
	try { neighbors = json(fixed_read('/usr/libexec/wifi-proxy/devices neighbors', 65536) || '[]'); } catch (e) {}
	if (bus) {
		for (let object in bus.list() || []) {
			if (index(object, 'hostapd.') != 0) continue;
			let clients = bus.call(object, 'get_clients', {});
			for (let mac, client in clients?.clients || {}) associations[mac] = client;
		}
	}
	return merge_devices(readfile('/tmp/dhcp.leases', 65536), type(neighbors) == 'array' ? neighbors : [], associations, blocked_list(uci), time());
}
function lan_cidr(address, netmask) {
	if (type(address) != 'string') return null;
	let fields = split(address, '/'), octets = split(fields[0], '.');
	if (length(octets) != 4) return null;
	let parts = [];
	for (let value in octets) { if (!match(value, /^\d{1,3}$/) || +value > 255) return null; push(parts, +value); }
	let prefix = fields[1] == null ? null : +fields[1];
	if (prefix == null) {
		let masks = split(netmask || '255.255.255.0', '.'), ended = false; prefix = 0;
		if (length(masks) != 4) return null;
		for (let value in masks) {
			if (!match(value, /^\d{1,3}$/) || +value > 255) return null;
			for (let bit = 7; bit >= 0; bit--) {
				if ((+value >> bit) & 1) { if (ended) return null; prefix++; } else ended = true;
			}
		}
	}
	if (prefix < 8 || prefix > 32 || prefix != int(prefix)) return null;
	for (let i = 0; i < 4; i++) { let bits = prefix - i * 8; parts[i] &= bits >= 8 ? 255 : bits <= 0 ? 0 : (255 << (8 - bits)) & 255; }
	return join('.', parts) + '/' + prefix;
}
function device_rules(blocked, lan, ipv6) {
	if (!lan || lan_cidr(lan, null) != lan) return null;
	let members = [];
	for (let value in blocked) { let mac = canonical_mac(value); if (!mac) return null; push(members, mac); }
	let local6 = ['fe80::/10', 'ff02::/16'];
	for (let prefix in ipv6 || []) if (type(prefix) == 'string' && match(prefix, /^[0-9a-fA-F:]+\/\d{1,3}$/) && +split(prefix, '/')[1] >= 48 && +split(prefix, '/')[1] <= 128) push(local6, prefix);
	return 'add table inet proxys22_devices\ndelete table inet proxys22_devices\nadd table inet proxys22_devices\n' +
		'add set inet proxys22_devices blocked { type ether_addr; }\n' +
		(length(members) ? 'add element inet proxys22_devices blocked { ' + join(', ', members) + ' }\n' : '') +
		'add chain inet proxys22_devices prerouting { type filter hook prerouting priority -151; policy accept; }\n' +
		'add rule inet proxys22_devices prerouting iifname "br-lan" ether saddr @blocked ip daddr { ' + lan + ', 224.0.0.0/24, 255.255.255.255 } return\n' +
		'add rule inet proxys22_devices prerouting iifname "br-lan" ether saddr @blocked ip6 daddr { ' + join(', ', local6) + ' } return\n' +
		'add rule inet proxys22_devices prerouting iifname "br-lan" ether saddr @blocked counter drop\n';
}
function apply_devices(uci, blocked) {
	let lan = lan_cidr(uci.get('network', 'lan', 'ipaddr'), uci.get('network', 'lan', 'netmask'));
	let ipv6 = [], bus;
	try {
		bus = require('ubus').connect(null, 1);
		let state = bus?.call('network.interface.lan', 'status', {});
		for (let item in state?.['ipv6-prefix-assignment'] || []) ipv6[length(ipv6)] = item.address + '/' + item.mask;
		bus?.disconnect();
	} catch (e) {}
	let rules = device_rules(blocked, lan, ipv6);
	if (!rules) return false;
	let process = popen('/usr/libexec/wifi-proxy/devices nft', 'w');
	if (!process) return false;
	let count = process.write(rules), code = process.close();
	return count == length(rules) && code == 0;
}
function devices_ready(uci) {
	let ready = read_json(runtime + '/devices-ready.json');
	return ready.ok === true && sprintf('%J', ready.blocked) == sprintf('%J', blocked_list(uci)) && access('/usr/libexec/wifi-proxy/devices', 'x') &&
		system(['/usr/libexec/wifi-proxy/devices', 'check'], 1000) == 0;
}
function device_command(args) {
	if (args[0] != 'apply' && args[0] != 'set') return 1;
	let mac = args[0] == 'set' ? canonical_mac(args[1]) : null;
	if (args[0] == 'set' && (!mac || (args[2] != '1' && args[2] != '0'))) return 1;
	let uci = require('uci').cursor(), old = blocked_list(uci), next = [];
	for (let value in old) if (value != mac) push(next, value);
	if (mac && args[2] == '1') push(next, mac);
	sort(next);
	if (!apply_devices(uci, next)) { save_json(runtime + '/devices-ready.json', { ok: false }); return 1; }
	if (args[0] == 'set') {
		if (length(next)) uci.set('wifi_proxy', 'main', 'blocked', next); else uci.delete('wifi_proxy', 'main', 'blocked');
		if (!uci.commit('wifi_proxy')) {
			apply_devices(uci, old); save_json(runtime + '/devices-ready.json', { ok: false }); return 1;
		}
	}
	return save_json(runtime + '/devices-ready.json', { ok: true, blocked: next }) ? 0 : 1;
}

export { read_json, canonical_mac, blocked_list, metrics_delta, sample_metrics, parse_iwinfo_scan, normalize_scan, scan_wifi, merge_devices, list_devices, lan_cidr, device_rules, devices_ready, device_command };
