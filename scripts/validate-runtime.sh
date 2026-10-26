#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
BUILDER=/opt/wifi-proxy/openwrt-imagebuilder-25.12.2-ramips-mt76x8.Linux-x86_64
SOURCE="$BUILDER/build_dir/target-mipsel_24kc_musl/root-ramips"
ROOT=/opt/wifi-proxy/runtime-check
[[ -d "$SOURCE" && "$ROOT" == /opt/wifi-proxy/runtime-check ]] || exit 1
command -v qemu-mipsel-static >/dev/null
mkdir -p "$ROOT"
rsync -a --delete "$SOURCE/" "$ROOT/"
mkdir -p "$ROOT/tmp"
cp /usr/bin/qemu-mipsel-static "$ROOT/tmp/"
# Only this disposable root gets a native stub. RPC tests cannot touch real networking.
printf 'int main(void) { return 0; }\n' | cc -static -x c -o "$ROOT/usr/libexec/wifi-proxy/apply" -
python3 "$REPO/tests/runtime-smoke.py" "$ROOT"

HOST_UCODE=/opt/wifi-proxy/ucode/build-host/ucode
if [[ -x "$HOST_UCODE" ]]; then
  "$HOST_UCODE" -L /opt/wifi-proxy/ucode/build-host "$REPO/tests/profile.test.uc"
  "$HOST_UCODE" -L /opt/wifi-proxy/ucode/build-host "$REPO/tests/client-config-smoke.uc" > /opt/wifi-proxy/smoke-client.json
  python3 - <<'PY'
import json
path = '/opt/wifi-proxy/smoke-client.json'
with open(path) as source:
    config = json.load(source)
# QEMU/WSL does not emulate the MIPS UDP TPROXY socket option. Validate the same
# client core and JSON fields with a loopback-only SOCKS listener instead.
config.pop('tcpTProxy')
config.pop('udpTProxy')
config['socks5'] = {'listen': '127.0.0.1:1088'}
with open(path, 'w') as target:
    json.dump(config, target)
PY
  set +e
  timeout 3s qemu-mipsel-static "$REPO/.build/cache/hysteria-linux-mipsle-sf" client \
    --config /opt/wifi-proxy/smoke-client.json --log-level warn > /opt/wifi-proxy/smoke-client.log 2>&1
  result=$?
  set -e
  if [[ "$result" != 124 ]]; then
    cat /opt/wifi-proxy/smoke-client.log
    exit 1
  fi
  printf 'PASS: Hysteria MIPS core startup (TPROXY requires device validation)\n'
fi
