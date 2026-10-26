# Upstream components

The firmware and terminal binaries include software from the projects below.
Their respective licenses and copyright notices continue to apply; publishing
this repository does not relicense those components.

- OpenWrt and its packages: https://openwrt.org/license — exact package versions
  are shipped in the firmware `.manifest`; source/build inputs are pinned in
  `config/build.json` and the build scripts.
- AmneziaWG kernel module and tools: repositories and commits in `config/amneziawg.json`.
- Hysteria: https://github.com/apernet/hysteria — MIT; pinned release/checksum in `config/build.json`.
- Python: https://docs.python.org/3/license.html — PSF License.
- Paramiko: https://github.com/paramiko/paramiko — LGPL-2.1.
- Cryptography: https://github.com/pyca/cryptography — Apache-2.0 OR BSD-3-Clause.
- bcrypt: https://github.com/pyca/bcrypt — Apache-2.0.
- PyNaCl: https://github.com/pyca/pynacl — Apache-2.0.
- cffi: https://foss.heptapod.net/pypy/cffi — MIT.
- Invoke: https://github.com/pyinvoke/invoke — BSD-2-Clause.
- PyInstaller: https://pyinstaller.org — GPL with the bootloader distribution exception.

Build-time package metadata and bundled license files remain part of their
upstream distributions. The terminal source is available in this repository;
replacing or rebuilding an included library is supported by installing the
requirements and running `scripts/package-manager.py` on the desired OS.
