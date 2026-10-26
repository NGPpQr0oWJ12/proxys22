// Hysteria URI decoding is kept independent of the router runtime for testing.
function fail(message) { die('WIFI_PROXY_SAFE:' + message); }

function decode(value, form) {
	if (form) value = replace(value, '+', ' ');
	if (index(replace(value, /%[0-9A-Fa-f]{2}/g, ''), '%') >= 0)
		fail('Некорректное кодирование ссылки.');
	return replace(value, /%([0-9A-Fa-f]{2})/g, (all, hex) => chr(int(hex, 16)));
}

function clean(value, limit) {
	return type(value) == 'string' && length(value) <= limit &&
		index(value, chr(0)) < 0 && index(value, '\r') < 0 && index(value, '\n') < 0;
}

function parse_profile(uri) {
	if (!clean(uri, 8192)) fail('Некорректная ссылка подключения.');
	let parts = match(trim(uri), /^(hy2|hysteria2):\/\/([^\/?#]+)\/?(\?[^#]*)?(#.*)?$/);
	if (!parts) fail('Нужна ссылка hy2:// или hysteria2://.');
	let authority = match(parts[2], /^(.+)@([A-Za-z0-9.-]+)(:([0-9]{1,5}))?$/);
	if (!authority || length(authority[2]) > 253)
		fail('Поддерживается имя сервера или IPv4 и один порт.');
	let auth = decode(authority[1], false);
	if (!length(auth) || !clean(auth, 2048)) fail('Некорректный ключ подключения.');
	let port = authority[4] ? int(authority[4]) : 443;
	if (port < 1 || port > 65535) fail('Порт вне диапазона.');
	let query = {};
	for (let entry in split(substr(parts[3] || '', 1), '&')) {
		if (!entry) continue;
		let pos = index(entry, '=');
		let key = decode(pos < 0 ? entry : substr(entry, 0, pos), true);
		let val = decode(pos < 0 ? '' : substr(entry, pos + 1), true);
		if (key in query) fail('Параметр ссылки повторяется.');
		query[key] = val;
	}
	let sni = query.sni || authority[2];
	if (!match(sni, /^[A-Za-z0-9.-]+$/) || length(sni) > 253)
		fail('Некорректное имя TLS-сервера.');
	let pin = replace(query.pinSHA256 || '', ':', '');
	if (pin && !match(pin, /^[A-Fa-f0-9]{64}$/)) fail('Некорректный отпечаток сертификата.');
	if ((query.insecure == '1' || query.insecure == 'true') && !pin)
		fail('Для этого профиля нужен pinSHA256 сертификата: проверка TLS обязательна.');
	if (query.obfs && query.obfs != 'salamander') fail('Поддерживается маскировка Salamander.');
	if (query.obfs && (!length(query['obfs-password'] || '') || !clean(query['obfs-password'], 2048)))
		fail('В профиле отсутствует ключ Salamander.');
	return {
		server: authority[2] + ':' + port, auth,
		sni, pin, obfs: query.obfs || '', obfs_password: query['obfs-password'] || ''
	};
}

function client_config(profile) {
	let config = {
		server: profile.server,
		auth: profile.auth,
		lazy: true,
		tls: { sni: profile.sni, insecure: !!profile.pin },
		quic: {
			// Chrome Parrot forces 6/15 MiB windows; use explicit limits on 128 MiB MIPS.
			disableChromeParrot: true,
			initStreamReceiveWindow: 2097152, maxStreamReceiveWindow: 2097152,
			initConnReceiveWindow: 5242880, maxConnReceiveWindow: 5242880
		},
		socks5: { listen: '127.0.0.1:1080' },
		tcpTProxy: { listen: '0.0.0.0:12345' },
		udpTProxy: { listen: '0.0.0.0:12345', timeout: '60s' },
		udpForwarding: [{ listen: '127.0.0.1:5353', remote: '1.1.1.1:53', timeout: '10s' }]
	};
	if (profile.pin) config.tls.pinSHA256 = profile.pin;
	if (profile.obfs)
		config.obfs = { type: 'salamander', salamander: { password: profile.obfs_password } };
	return config;
}

export { parse_profile, client_config };
