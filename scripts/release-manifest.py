#!/usr/bin/env python3
"""Create the firmware catalog consumed by the terminal manager."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from proxys22_manager.releases import MAX_IMAGE_BYTES, PROFILE, TARGET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    config = json.loads((ROOT / 'config/build.json').read_text(encoding='utf-8-sig'))
    if config['brand'] != 'proxys22' or config['openwrt']['profile'] != PROFILE or config['openwrt']['target'] != TARGET:
        parser.error('Unsupported build target')
    version = config['version']
    prefix = f'proxys22-{version}-{PROFILE}'
    images = {}
    for kind in ('factory', 'sysupgrade'):
        path = args.dist / f'{prefix}-{kind}.bin'
        if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            parser.error('Image exceeds the board size limit')
        images[kind] = {'name': path.name, 'size': path.stat().st_size,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {'schema': 1, 'brand': 'proxys22', 'version': version, 'profile': PROFILE,
                'target': TARGET, 'openwrt_version': config['openwrt']['version'], 'images': images}
    destination = args.dist / 'proxys22-release.json'
    destination.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    names = [images[kind]['name'] for kind in images] + [f'{prefix}.manifest', 'build.json', 'amneziawg.json', destination.name]
    checksums = ''.join(f'{hashlib.sha256((args.dist / name).read_bytes()).hexdigest()}  {name}\n' for name in names)
    (args.dist / 'sha256sums').write_text(checksums, encoding='ascii')
    print(f'Release manifest: {destination}')


if __name__ == '__main__':
    main()
