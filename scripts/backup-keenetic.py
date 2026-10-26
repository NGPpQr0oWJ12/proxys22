"""Read-only LAN backup. Credentials are read from the environment, never printed."""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    password = os.environ.get('WIFI_PROXY_ROUTER_PASSWORD')
    if not password:
        raise SystemExit('Missing WIFI_PROXY_ROUTER_PASSWORD.')
    base = 'http://' + args.host
    session = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )

    def get(path):
        with session.open(base + path, timeout=15) as response:
            return response.read(), response.headers.get('Content-Type', '')

    try:
        get('/auth')
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise SystemExit('Router authentication endpoint failed.')
        realm = error.headers.get('X-NDM-Realm')
        challenge = error.headers.get('X-NDM-Challenge')
        if not realm or not challenge:
            raise SystemExit('Unsupported authentication challenge.')
        digest = hashlib.md5(('admin:' + realm + ':' + password).encode()).hexdigest()
        key = hashlib.sha256((challenge + digest).encode()).hexdigest()
        request = urllib.request.Request(
            base + '/auth', json.dumps({'login': 'admin', 'password': key}).encode(),
            {'Content-Type': 'application/json'}, method='POST',
        )
        with session.open(request, timeout=15) as response:
            response.read()

    version_raw, _ = get('/rci/show/version')
    version = json.loads(version_raw)
    if version.get('hw_id') != 'KN-1112':
        raise SystemExit('Backup stopped: unexpected router model.')
    config_raw, _ = get('/rci/')
    if not isinstance(json.loads(config_raw), (dict, list)) or len(config_raw) < 100:
        raise SystemExit('Backup stopped: empty configuration.')
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'version.json').write_bytes(version_raw)
    (args.output / 'running-config.json').write_bytes(config_raw)
    saved = ['version.json', 'running-config.json']
    for endpoint, filename in [('/ci/startup-config', 'startup-config'), ('/ci/firmware', 'keenetic-firmware.bin')]:
        try:
            body, content_type = get(endpoint)
            if len(body) < 50 or 'html' in content_type.lower() or body.lstrip().startswith(b'<'):
                continue
            (args.output / filename).write_bytes(body)
            saved.append(filename)
        except (urllib.error.URLError, TimeoutError):
            continue
    checksums = []
    for filename in saved:
        data = (args.output / filename).read_bytes()
        checksums.append(hashlib.sha256(data).hexdigest() + '  ' + filename)
        print(f'Backed up {filename}: {len(data)} bytes')
    (args.output / 'sha256sums').write_text('\n'.join(checksums) + '\n', encoding='ascii')
    print('Backup checksums saved; router configuration was not changed.')


if __name__ == '__main__':
    try:
        main()
    except urllib.error.HTTPError as error:
        raise SystemExit(f'Backup HTTP error: {error.code}') from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise SystemExit('Backup could not be completed; no router changes made.') from None
