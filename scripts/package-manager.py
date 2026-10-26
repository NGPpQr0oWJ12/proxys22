#!/usr/bin/env python3
"""Build the terminal executable on the target OS; never contacts hardware."""
import argparse
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from proxys22_manager import VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/manager')
    args = parser.parse_args()
    system = {'Windows': 'windows', 'Linux': 'linux'}.get(platform.system())
    machine = {'AMD64': 'x86_64', 'x86_64': 'x86_64', 'aarch64': 'arm64', 'ARM64': 'arm64'}.get(platform.machine())
    if system is None or machine is None:
        parser.error('This build supports Windows/Linux x86_64 or arm64.')
    name = f'proxys22-{VERSION}-{system}-{machine}'
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
                    '--console', '--name', name, '--distpath', str(args.output.resolve()),
                    '--workpath', str(ROOT / '.build/manager-work'),
                    '--specpath', str(ROOT / '.build/manager-spec'),
                    '--recursive-copy-metadata', 'paramiko',
                    '--add-data', f'{ROOT / "THIRD_PARTY.md"}:.',
                    '--add-data', f'{ROOT / "scripts/serve-recovery.py"}:scripts',
                    str(ROOT / 'proxys22.py')], cwd=ROOT, check=True)
    suffix = '.exe' if system == 'windows' else ''
    executable = args.output.resolve() / (name + suffix)
    subprocess.run([str(executable), '--version'], check=True)
    subprocess.run([str(executable), 'doctor'], check=True)
    print(executable)


if __name__ == '__main__':
    main()
