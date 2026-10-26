#!/usr/bin/env bash
# Host-only updater validation. No router access or firmware installation.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 022

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
COMMIT=85922056ef7abeace3cca3ab28bc1ac2d88e31b1
SHA256=54dda1e8a2757710c285659e42336f5054f1825f20d01a4016245bbf877f6da7
ROOT=/opt/wifi-proxy
SOURCE="$ROOT/ucode-source-$COMMIT"
BUILD="$ROOT/ucode/build-host"
ARCHIVE="$ROOT/ucode-$COMMIT.tar.gz"
LOG="$ROOT/host-ucode-build.log"

fail() { printf 'Updater validation: %s\n' "$*" >&2; exit 1; }
[[ "$EUID" == 0 ]] || fail 'Run with sudo: the isolated updater/chroot tests require root ownership.'
for tool in python3 curl cmake make cc pkg-config sha256sum tar realpath; do
  command -v "$tool" >/dev/null || fail "Missing host dependency: $tool"
done
mkdir -p -- "$ROOT"
[[ "$(realpath -e -- "$ROOT")" == "$ROOT" ]] || fail 'The build root must be /opt/wifi-proxy, without symlink redirection.'
for path in "$SOURCE" "$BUILD" "$ARCHIVE" "$ARCHIVE.part" "$LOG"; do
  [[ "$(realpath -m -- "$path")" == "$path" ]] || fail 'A build/cache path redirects outside its fixed location.'
done

trap 'rm -f -- "$ARCHIVE.part"' EXIT
if [[ ! -f "$ARCHIVE" ]]; then
  curl --fail --location --silent --show-error --connect-timeout 15 --max-time 120 \
    --proto '=https' --proto-redir '=https' \
    "https://codeload.github.com/jow-/ucode/tar.gz/$COMMIT" -o "$ARCHIVE.part"
  printf '%s  %s\n' "$SHA256" "$ARCHIVE.part" | sha256sum -c - >/dev/null || fail 'The pinned ucode archive failed SHA-256 verification.'
  mv -- "$ARCHIVE.part" "$ARCHIVE"
fi
printf '%s  %s\n' "$SHA256" "$ARCHIVE" | sha256sum -c - >/dev/null || fail 'The cached ucode archive failed SHA-256 verification.'
printf 'Verified upstream ucode %s archive.\n' "$COMMIT"

if [[ ! -x "$BUILD/ucode" || ! -f "$BUILD/fs.so" ]]; then
  mkdir -p -- "$SOURCE" "$BUILD"
  tar -xzf "$ARCHIVE" --strip-components=1 -C "$SOURCE"
  if ! cmake -S "$SOURCE" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -DCOMPILE_SUPPORT=ON \
    -DFS_SUPPORT=ON -DUBUS_SUPPORT=OFF -DUCI_SUPPORT=OFF -DULOOP_SUPPORT=OFF \
    -DRTNL_SUPPORT=OFF -DNL80211_SUPPORT=OFF -DDIGEST_SUPPORT=OFF -DZLIB_SUPPORT=OFF >"$LOG" 2>&1; then
    tail -n 60 -- "$LOG" >&2
    fail 'Host ucode configuration failed.'
  fi
  if ! cmake --build "$BUILD" --target ucode fs_lib --parallel 2 >>"$LOG" 2>&1; then
    tail -n 60 -- "$LOG" >&2
    fail 'Host ucode/fs compilation failed.'
  fi
fi
"$BUILD/ucode" -L "$BUILD" -e 'import { stat, chmod, lstat } from "fs"; if (type(stat) != "function" || type(chmod) != "function" || type(lstat) != "function" || b64enc("test") != "dGVzdA==") die("Host ucode/fs is unavailable");'
printf 'Native ucode/fs runtime ready; running isolated updater tests.\n'
cd -- "$REPO"
python3 -m unittest discover -s tests -p test_web_update.py
