// Import data only. No wg-quick hooks, commands, arbitrary routes or extra peers.
function fail() { die('WIFI_PROXY_SAFE:Нужен IPv4-конфиг AmneziaWG с одним сервером, без команд PostUp/PostDown.'); }
function ipv4(value) {
	let parts = split(value, '.');
	if (length(parts) != 4) return false;
	for (let part in parts) if (!match(part, /^(0|[1-9][0-9]{0,2})$/) || int(part) > 255) return false;
	return true;
}
function key(value) { return !!match(value, /^[A-Za-z0-9+\/]{43}=$/); }
function parse_awg(raw) {
	if (type(raw) != 'string' || length(raw) > 8192 || index(raw, chr(0)) >= 0) fail();
	let section = '', values = { interface: {}, peer: {} }, seen = {};
	for (let line in split(replace(raw, '\r', ''), '\n')) {
		line = trim(split(line, '#')[0]);
		if (!line) continue;
		let heading = match(line, /^\[(Interface|Peer)\]$/);
		if (heading) {
			section = lc(heading[1]);
			if (seen[section]) fail();
			seen[section] = true; continue;
		}
		let entry = match(line, /^([A-Za-z0-9]+)\s*=\s*(.+)$/);
		if (!section || !entry) fail();
		let name = lc(entry[1]), value = trim(entry[2]);
		if (name in values[section]) fail();
		let allowed = section == 'interface' ? ['privatekey', 'address', 'dns', 'mtu', 'jc', 'jmin', 'jmax', 's1', 's2', 's3', 's4', 'h1', 'h2', 'h3', 'h4'] : ['publickey', 'presharedkey', 'allowedips', 'endpoint', 'persistentkeepalive'];
		if (index(allowed, name) < 0) fail();
		values[section][name] = value;
	}
	let iface = values.interface, peer = values.peer;
	if (!key(iface.privatekey || '') || !key(peer.publickey || '') ||
		(peer.presharedkey && !key(peer.presharedkey)) || peer.allowedips != '0.0.0.0/0') fail();
	let address = match(iface.address || '', /^([0-9.]+)\/32$/);
	if (!address || !ipv4(address[1])) fail();
	let octets = split(address[1], '.');
	if (int(octets[0]) != 10 && !(int(octets[0]) == 172 && int(octets[1]) >= 16 && int(octets[1]) <= 31) && !(int(octets[0]) == 192 && int(octets[1]) == 168)) fail();
	let endpoint = match(peer.endpoint || '', /^([A-Za-z0-9][A-Za-z0-9.-]{0,252}):([0-9]{1,5})$/);
	if (!endpoint || int(endpoint[2]) < 1 || int(endpoint[2]) > 65535) fail();
	if (iface.dns) for (let dns in split(iface.dns, ',')) if (!ipv4(trim(dns))) fail();
	let bounds = { mtu: [1280, 1420], jc: [0, 128], jmin: [0, 1280], jmax: [0, 1280], s1: [0, 1280], s2: [0, 1280], s3: [0, 1280], s4: [0, 1280] };
	for (let name, range in bounds) if (iface[name] && (!match(iface[name], /^\d+$/) || int(iface[name]) < range[0] || int(iface[name]) > range[1])) fail();
	if (int(iface.jmin || '0') > int(iface.jmax || '0')) fail();
	for (let name in ['h1', 'h2', 'h3', 'h4']) if (iface[name]) {
		let range = match(iface[name], /^(\d+)(-(\d+))?$/);
		if (!range || int(range[1]) > 4294967295 || (range[3] && (int(range[3]) > 4294967295 || int(range[1]) > int(range[3])))) fail();
	}
	if (peer.persistentkeepalive && (!match(peer.persistentkeepalive, /^\d+$/) || int(peer.persistentkeepalive) > 65535)) fail();
	let config = '[Interface]\n';
	for (let name, value in iface) if (index(['address', 'dns', 'mtu'], name) < 0) config += name + ' = ' + value + '\n';
	config += 'FwMark = 512\n[Peer]\n';
	for (let name, value in peer) config += name + ' = ' + value + '\n';
	return { raw, server: peer.endpoint, address: address[1], mtu: int(iface.mtu || '1420'), config };
}
export { parse_awg };
