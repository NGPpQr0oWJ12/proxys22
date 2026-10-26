// Synthetic configuration only: this file never contains a real subscription.
import { parse_profile, client_config } from '../overlay/usr/share/wifi-proxy/profile.uc';
let config = client_config(parse_profile('hy2://smoke-test@127.0.0.1:1?sni=gateway.example'));
config.tcpTProxy.listen = '127.0.0.1:12345';
config.udpTProxy.listen = '127.0.0.1:12345';
printf('%J\n', config);
