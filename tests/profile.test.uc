import { parse_profile, client_config } from '../overlay/usr/share/wifi-proxy/profile.uc';

let count = 0;
function check(condition, name) {
	if (!condition) die('FAIL: ' + name);
	count++;
}
function rejects(uri, name) {
	let rejected = false;
	try { parse_profile(uri); } catch (e) { rejected = true; }
	check(rejected, name);
}

let profile = parse_profile('hy2://a+b%2Bc@gateway.example:8443/?sni=tls.example&obfs=salamander&obfs-password=a+b%2Bc#Router');
check(profile.auth == 'a+b+c', 'userinfo preserves raw plus');
check(profile.obfs_password == 'a b+c', 'query form decoding');
check(profile.server == 'gateway.example:8443', 'server and port');
check(profile.sni == 'tls.example', 'TLS name');
check(parse_profile('hysteria2://%C3%A9@gateway.example').auth == 'é', 'UTF-8 percent encoding');
check(parse_profile('hy2://a%252Bb@gateway.example').auth == 'a%2Bb', 'decode once');
check(parse_profile('hy2://a%40b%3Ac@gateway.example').auth == 'a@b:c', 'decode after structural split');
check(parse_profile('hy2://key@gateway.example').server == 'gateway.example:443', 'default port');

let config = client_config(profile);
check(config.tls.insecure == false, 'TLS verification defaults on');
check(config.lazy == true, 'start listeners before upstream is available');
check(config.tcpTProxy.listen == '0.0.0.0:12345' && config.udpTProxy.listen == '0.0.0.0:12345', 'TCP and UDP capture');
check(config.udpForwarding[0].listen == '127.0.0.1:5353', 'DNS through tunnel');
check(config.quic.maxConnReceiveWindow == 5242880, 'bounded QUIC window');
check(config.socks5.listen == '127.0.0.1:1080', 'health probe SOCKS remains loopback only');
check(config.quic.disableChromeParrot == true, 'Chrome window override disabled on low-memory MIPS');
check(config.obfs.salamander.password == 'a b+c', 'Salamander configuration');

let hash = '';
for (let i = 0; i < 32; i++) hash += 'ab';
let pinned = client_config(parse_profile('hy2://key@gateway.example?insecure=1&pinSHA256=' + hash));
check(pinned.tls.insecure && length(pinned.tls.pinSHA256) == 64, 'self-signed TLS requires fingerprint');
rejects('socks://key@gateway.example', 'unsupported protocol');
rejects('hy2://key@gateway.example:65536', 'invalid port');
rejects('hy2://key@gateway.example:0', 'zero port');
rejects('hy2://bad%GG@gateway.example', 'malformed percent escape');
rejects('hy2://bad%00@gateway.example', 'NUL auth');
rejects('hy2://bad%0A@gateway.example', 'newline auth');
rejects('hy2://key@gateway.example?insecure=1', 'unverified TLS rejected');
rejects('hy2://key@gateway.example?sni=a&sni=b', 'duplicate query');
rejects('hy2://key@gateway.example?sni=bad%0Aname', 'TLS name validation');
rejects('hy2://key@gateway.example?obfs=salamander', 'missing obfuscation key');
rejects('hy2://key@gateway.example?obfs=unknown', 'unknown obfuscation');
printf('PASS: %d profile checks\n', count);
