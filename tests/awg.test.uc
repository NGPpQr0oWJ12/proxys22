import { parse_awg } from '../overlay/usr/share/wifi-proxy/awg.uc';
let tests = 0;
function check(ok) { if (!ok) die('AWG assertion failed'); tests++; }
const sample = '[Interface]\nPrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\nAddress = 10.77.22.2/32\nDNS = 1.1.1.1\nMTU = 1420\nJc = 3\nJmin = 20\nJmax = 80\nS1 = 23\nS2 = 41\nH1 = 123456\nH2 = 123457\nH3 = 123458\nH4 = 123459\n[Peer]\nPublicKey = BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=\nPresharedKey = CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC=\nAllowedIPs = 0.0.0.0/0\nEndpoint = gateway.example:54433\nPersistentKeepalive = 25\n';
let p = parse_awg(sample);
check(p.address == '10.77.22.2' && p.mtu == 1420 && p.server == 'gateway.example:54433');
check(index(p.config, 'Address') < 0 && index(p.config, 'DNS') < 0 && index(p.config, 'FwMark = 512') >= 0);
check(parse_awg(replace(sample, '\n', '\r\n')).config == p.config);
for (let invalid in [
	replace(sample, 'Jc = 3', 'PostUp = arbitrary command'),
	sample + '[Peer]\nPublicKey = anything',
	replace(sample, 'MTU = 1420', 'MTU = 9000'),
	replace(sample, 'Jmin = 20', 'Jmin = 81'),
	replace(sample, 'H1 = 123456', 'H1 = 4294967296'),
	replace(sample, '10.77.22.2/32', '127.0.0.1/32'),
	replace(sample, '10.77.22.2/32', '10.77.22.999/32'),
	replace(sample, 'gateway.example:54433', 'gateway.example:0'),
	replace(sample, '0.0.0.0/0', '::/0'),
	replace(sample, 'Jc = 3', 'Jc = 3\nJc = 2'),
	replace(sample, 'DNS = 1.1.1.1', 'DNS = x; command')
]) {
	let rejected = false;
	try { parse_awg(invalid); } catch (e) { rejected = true; }
	check(rejected);
}
printf('AWG parser: %d checks passed\n', tests);
