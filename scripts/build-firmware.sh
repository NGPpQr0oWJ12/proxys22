#!/usr/bin/env bash
# Build artifacts only. This script never connects to or flashes a router.
set -euo pipefail
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
CONFIG="$REPO_ROOT/config/build.json"
CACHE="$REPO_ROOT/.build/cache"
WORK_ROOT="${WIFI_PROXY_BUILD_ROOT:-/opt/wifi-proxy}"

fail() { printf 'Build error: %s\n' "$*" >&2; exit 1; }
pin() {
  python3 - "$CONFIG" "$1" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8-sig") as source:
    value = json.load(source)
for key in sys.argv[2].split("."):
    value = value[key]
print(" ".join(value) if isinstance(value, list) else value)
PY
}

for tool in python3 curl sha256sum tar zstd rsync make realpath stat find install; do
  command -v "$tool" >/dev/null || fail "Missing dependency: $tool"
done
[[ -f "$CONFIG" && -d "$REPO_ROOT/overlay" ]] || fail 'Build pins or overlay are missing.'
WORK_ROOT="$(realpath -m -- "$WORK_ROOT")"
[[ "$WORK_ROOT" == /opt/* ]] || fail 'Build workspace must be inside /opt on Linux ext4.'
mkdir -p -- "$WORK_ROOT" "$CACHE"
case "$(stat -f -c %T -- "$WORK_ROOT")" in
  ext2/ext3|ext4) ;;
  *) fail 'Build workspace must use Linux ext4; /mnt/NTFS builds are unsupported.' ;;
esac

BRAND="$(pin brand)"
VERSION="$(pin version)"
OPENWRT_VERSION="$(pin openwrt.version)"
TARGET="$(pin openwrt.target)"
PROFILE="$(pin openwrt.profile)"
LIMIT="$(pin openwrt.factory_max_bytes)"
SOURCE_DATE_EPOCH="$(pin source_date_epoch)"
PACKAGES="$(pin openwrt.packages)"
[[ "$BRAND-$VERSION" =~ ^[a-z0-9][a-z0-9.-]+$ ]] || fail 'Invalid firmware brand/version.'
[[ "$OPENWRT_VERSION" == 25.12.2 && "$TARGET" == ramips/mt76x8 && "$PROFILE" == keenetic_kn-1112 ]] || fail 'Unsupported firmware target.'
[[ "$LIMIT" == 16121856 && "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || fail 'Invalid image limit or timestamp pin.'
export SOURCE_DATE_EPOCH LC_ALL=C TZ=UTC

ensure_download() {
  local url="$1" destination="$2" expected="$3" actual temporary
  [[ "$url" == https://* && "$expected" =~ ^[0-9a-f]{64}$ ]] || fail 'Invalid download pin.'
  if [[ ! -f "$destination" ]]; then
    temporary="$(mktemp "$CACHE/download.XXXXXX")"
    if ! curl --fail --location --silent --show-error --retry 3 --proto '=https' --tlsv1.2 "$url" -o "$temporary"; then
      rm -f -- "$temporary"
      fail 'Pinned download failed.'
    fi
    actual="$(sha256sum "$temporary")"
    if [[ "${actual%% *}" != "$expected" ]]; then
      rm -f -- "$temporary"
      fail 'Downloaded file failed SHA256 verification.'
    fi
    mv -- "$temporary" "$destination"
  fi
  actual="$(sha256sum "$destination")"
  [[ "${actual%% *}" == "$expected" ]] || fail "Cached file failed SHA256 verification: $(basename "$destination")"
}

printf 'Verifying OpenWrt %s and Hysteria %s pins...\n' "$OPENWRT_VERSION" "$(pin hysteria.version)"
ensure_download "$(pin openwrt.imagebuilder.url)" "$CACHE/imagebuilder.tar.zst" "$(pin openwrt.imagebuilder.sha256)"
ensure_download "$(pin hysteria.url)" "$CACHE/hysteria-linux-mipsle-sf" "$(pin hysteria.sha256)"

BUILDER_NAME="openwrt-imagebuilder-$OPENWRT_VERSION-${TARGET//\//-}.Linux-x86_64"
BUILDER="$WORK_ROOT/$BUILDER_NAME"
if [[ ! -f "$BUILDER/Makefile" ]]; then
  tar --zstd -xf "$CACHE/imagebuilder.tar.zst" -C "$WORK_ROOT"
fi
[[ -f "$BUILDER/Makefile" ]] || fail 'Pinned ImageBuilder could not be unpacked.'

# Only overlay/ is a firmware source. Never copy the repository or .private/.
[[ -z "$(find "$REPO_ROOT/overlay" -type l -print -quit)" ]] || fail 'Overlay symlinks are unsupported.'
FILES="$WORK_ROOT/files"
mkdir -p -- "$FILES"
rsync -rlt --delete --exclude='.private' --exclude='.git' "$REPO_ROOT/overlay/" "$FILES/"
python3 - "$FILES" "$VERSION" <<'PY'
import hashlib, pathlib, re, sys
for path in pathlib.Path(sys.argv[1]).rglob("*"):
    if not path.is_file():
        continue
    data = path.read_bytes()
    if b"\0" in data or any(byte < 9 or 13 < byte < 32 for byte in data):
        continue
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        continue
    if b"\r\n" in data:
        path.write_bytes(data.replace(b"\r\n", b"\n"))
# Firmware timestamps are reproducible. Content stamps prevent a browser from
# reusing an older script/style after a system upgrade returns HTTP 304.
web = pathlib.Path(sys.argv[1]) / "www"
index = web / "index.html"
html = index.read_text(encoding="utf-8")
for asset in ("app.js", "update.js", "style.css", "logo.png"):
    stamp = hashlib.sha256((web / "assets" / asset).read_bytes()).hexdigest()[:12]
    html = re.sub(r'/assets/' + re.escape(asset) + r'(?:\?[^"\s]*)?',
                  f'/assets/{asset}?v={sys.argv[2]}-{stamp}', html)
index.write_text(html, encoding="utf-8")
PY
find "$FILES" -type d -exec chmod 0755 {} +
find "$FILES" -type f -exec chmod 0644 {} +
for directory in usr/bin usr/sbin usr/libexec usr/lib/rpcd etc/init.d etc/uci-defaults; do
  if [[ -d "$FILES/$directory" ]]; then
    find "$FILES/$directory" -type f -exec chmod 0755 {} +
  fi
done
# Install the binary after text normalization, without ever decoding or editing it.
install -D -m 0755 "$CACHE/hysteria-linux-mipsle-sf" "$FILES/usr/bin/hysteria"
bash "$REPO_ROOT/scripts/build-amneziawg.sh"
AWG_KERNEL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["kernel"])' "$REPO_ROOT/config/amneziawg.json")"
install -D -m 0644 "$REPO_ROOT/.build/awg/amneziawg.ko" "$FILES/lib/modules/$AWG_KERNEL/amneziawg.ko"
install -D -m 0755 "$REPO_ROOT/.build/awg/awg" "$FILES/usr/bin/awg"
find "$FILES" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +

OUTPUT="$WORK_ROOT/output"
LOG="$WORK_ROOT/build.log"
mkdir -p -- "$OUTPUT"
find "$OUTPUT" -maxdepth 1 -type f -delete
printf 'Building %s %s for %s (log: %s)...\n' "$BRAND" "$VERSION" "$PROFILE" "$LOG"
if ! make -C "$BUILDER" image PROFILE="$PROFILE" FILES="$FILES" PACKAGES="$PACKAGES" \
  EXTRA_IMAGE_NAME="$BRAND-$VERSION" BIN_DIR="$OUTPUT" >"$LOG" 2>&1; then
  tail -n 65 -- "$LOG" >&2
  fail 'ImageBuilder failed; see the log above.'
fi

shopt -s nullglob
factory=("$OUTPUT"/*"$PROFILE"*squashfs-factory.bin)
sysupgrade=("$OUTPUT"/*"$PROFILE"*squashfs-sysupgrade.bin)
manifest=("$OUTPUT"/*"$PROFILE"*.manifest)
if [[ ${#factory[@]} != 1 || ${#sysupgrade[@]} != 1 || ${#manifest[@]} != 1 ]]; then
  tail -n 45 -- "$LOG" >&2
  fail 'Expected exactly one factory image, sysupgrade image and package manifest.'
fi
FACTORY_BYTES="$(stat -c %s -- "${factory[0]}")"
SYSUPGRADE_BYTES="$(stat -c %s -- "${sysupgrade[0]}")"
(( FACTORY_BYTES > 0 && FACTORY_BYTES <= LIMIT )) || fail "Factory image is $FACTORY_BYTES bytes; limit is $LIMIT bytes."
(( SYSUPGRADE_BYTES > 0 && SYSUPGRADE_BYTES <= LIMIT )) || fail "Sysupgrade image is $SYSUPGRADE_BYTES bytes; limit is $LIMIT bytes."

DIST="$REPO_ROOT/dist"
PREFIX="$BRAND-$VERSION-$PROFILE"
mkdir -p -- "$DIST"
install -m 0644 "${factory[0]}" "$DIST/$PREFIX-factory.bin"
install -m 0644 "${sysupgrade[0]}" "$DIST/$PREFIX-sysupgrade.bin"
install -m 0644 "${manifest[0]}" "$DIST/$PREFIX.manifest"
install -m 0644 "$CONFIG" "$DIST/build.json"
install -m 0644 "$REPO_ROOT/config/amneziawg.json" "$DIST/amneziawg.json"
(cd -- "$DIST" && sha256sum "$PREFIX-factory.bin" "$PREFIX-sysupgrade.bin" "$PREFIX.manifest" build.json amneziawg.json > sha256sums)
python3 "$REPO_ROOT/scripts/release-manifest.py" --dist "$DIST"
printf 'Factory:    %s bytes / %s maximum\nSysupgrade: %s bytes\nArtifacts:  %s\n' "$FACTORY_BYTES" "$LIMIT" "$SYSUPGRADE_BYTES" "$DIST"
printf 'Build complete. No router was contacted or flashed.\n'
