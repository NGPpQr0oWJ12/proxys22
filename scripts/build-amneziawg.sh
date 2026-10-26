#!/usr/bin/env bash
# Native MIPS client, compiled against the exact OpenWrt SDK kernel. No router access.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
WORK="$(realpath -m -- "${WIFI_PROXY_BUILD_ROOT:-/opt/wifi-proxy}/awg")"
[[ "$WORK" == /opt/* ]] || exit 1
mkdir -p "$WORK" "$ROOT/.build/awg"
case "$(stat -f -c %T "$WORK")" in ext2/ext3|ext4) ;; *) exit 1 ;; esac
pin() { python3 - "$ROOT/config/amneziawg.json" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for key in sys.argv[2].split('.'): value = value[key]
print(value)
PY
}
fetch() {
  local url="$1" path="$2" hash="$3"
  if [[ ! -f "$path" ]]; then
    curl --fail --location --silent --show-error --retry 3 --proto '=https' "$url" -o "$path.part"
    printf '%s  %s\n' "$hash" "$path.part" | sha256sum -c - >/dev/null
    mv "$path.part" "$path"
  fi
  printf '%s  %s\n' "$hash" "$path" | sha256sum -c - >/dev/null
}
SDK_NAME="$(pin sdk.name)"; SDK="$WORK/$SDK_NAME"
fetch "$(pin sdk.url)" "$WORK/$SDK_NAME.tar.zst" "$(pin sdk.sha256)"
[[ -f "$SDK/Makefile" ]] || tar --zstd -xf "$WORK/$SDK_NAME.tar.zst" -C "$WORK"
for source in module tools; do
  repo="$(pin "$source.repository")"; commit="$(pin "$source.commit")"
  fetch "https://codeload.github.com/amnezia-vpn/$repo/tar.gz/$commit" "$WORK/$repo-$commit.tar.gz" "$(pin "$source.sha256")"
  [[ -d "$WORK/$repo-$commit" ]] || tar -xzf "$WORK/$repo-$commit.tar.gz" -C "$WORK"
done
KERNEL="$SDK/build_dir/target-mipsel_24kc_musl/linux-ramips_mt76x8/linux-$(pin kernel)"
[[ -f "$KERNEL/Module.symvers" && -f "$KERNEL/.config" ]] || { echo 'Matching prepared SDK kernel missing' >&2; exit 1; }
MODULE="$WORK/$(pin module.repository)-$(pin module.commit)/src"
TOOLS="$WORK/$(pin tools.repository)-$(pin tools.commit)/src"
TC="$(find "$SDK/staging_dir" -maxdepth 1 -type d -name 'toolchain-*' -print -quit)"
export STAGING_DIR="$SDK/staging_dir" PATH="$SDK/staging_dir/host/bin:$TC/bin:$PATH"
export KBUILD_BUILD_USER=proxys22 KBUILD_BUILD_HOST=builder KBUILD_BUILD_VERSION=1
export KBUILD_BUILD_TIMESTAMP="$(date -u -d "@${SOURCE_DATE_EPOCH:-1792972800}" '+%a %b %d %T %Z %Y')"
[[ -e "$MODULE/kernel" ]] || ln -s "$KERNEL" "$MODULE/kernel"
if ! make -C "$KERNEL" M="$MODULE" ARCH=mips CROSS_COMPILE="$TC/bin/mipsel-openwrt-linux-musl-" -j4 modules >"$WORK/module-build.log" 2>&1; then
  tail -n 25 "$WORK/module-build.log" >&2; exit 1
fi
make -C "$TOOLS" clean >/dev/null
export CFLAGS='-Os -ffunction-sections -fdata-sections' LDFLAGS='-Wl,--gc-sections -s'
if ! make -C "$TOOLS" CC="$TC/bin/mipsel-openwrt-linux-musl-gcc" WITH_BASHCOMPLETION=no WITH_SYSTEMDUNITS=no WITH_WGQUICK=no -j4 >"$WORK/tools-build.log" 2>&1; then
  tail -n 25 "$WORK/tools-build.log" >&2; exit 1
fi
"$TC/bin/mipsel-openwrt-linux-musl-strip" --strip-debug "$MODULE/amneziawg.ko" -o "$ROOT/.build/awg/amneziawg.ko"
install -m 0755 "$TOOLS/wg" "$ROOT/.build/awg/awg"
echo "Native AmneziaWG built for kernel $(pin kernel) / mipsel_24kc"
