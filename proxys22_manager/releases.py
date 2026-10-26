"""Public GitHub release discovery and verified firmware downloads."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_REPO = 'NGPpQr0oWJ12/proxys22'
PROFILE = 'keenetic_kn-1112'
TARGET = 'ramips/mt76x8'
MAX_IMAGE_BYTES = 16121856
MANIFEST_NAME = 'proxys22-release.json'


class ReleaseError(ValueError):
    pass


@dataclass(frozen=True)
class FirmwareImage:
    kind: str
    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    images: dict


def validate_repo(repo):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', repo):
        raise ReleaseError('Repository must be owner/name.')
    return repo


def asset_url(asset, repo, tag, name):
    expected = f'https://github.com/{validate_repo(repo)}/releases/download/{quote(tag, safe="")}/{quote(name, safe="")}'
    if asset.get('browser_download_url') != expected:
        raise ReleaseError('Release asset has an unexpected download URL.')
    return expected


def parse_manifest(data, tag, assets, repo=DEFAULT_REPO):
    if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
        raise ReleaseError('Unsupported release manifest schema.')
    version = data.get('version', '')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version) or tag != 'v' + version:
        raise ReleaseError('Firmware version does not match the release tag.')
    if data.get('brand') != 'proxys22' or data.get('profile') != PROFILE or data.get('target') != TARGET:
        raise ReleaseError('This firmware is not for proxys22 / Keenetic KN-1112.')
    if not isinstance(data.get('images'), dict):
        raise ReleaseError('Firmware images are missing from the manifest.')
    by_name = {item.get('name'): item for item in assets if isinstance(item, dict)}
    images = {}
    for kind in ('factory', 'sysupgrade'):
        image = data['images'].get(kind)
        name = f'proxys22-{version}-{PROFILE}-{kind}.bin'
        if not isinstance(image, dict) or image.get('name') != name:
            raise ReleaseError(f'Missing or invalid {kind} image.')
        size, digest = image.get('size'), image.get('sha256')
        if type(size) is not int or not 0 < size <= MAX_IMAGE_BYTES:
            raise ReleaseError('Firmware size is invalid or exceeds the device limit.')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ReleaseError('Invalid firmware SHA-256.')
        asset = by_name.get(name)
        if asset is None or type(asset.get('size')) is not int or asset['size'] != size:
            raise ReleaseError(f'{kind} asset is missing or its size does not match.')
        images[kind] = FirmwareImage(kind, name, size, digest, asset_url(asset, repo, tag, name))
    return Release(version, tag, images)


def verify_image(path, image):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != image.size:
        raise ReleaseError('Firmware file size does not match the release manifest.')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(65536), b''):
            digest.update(block)
    if digest.hexdigest() != image.sha256:
        raise ReleaseError('Firmware SHA-256 verification failed.')


class Catalog:
    def __init__(self, repo=DEFAULT_REPO, opener=None):
        self.repo = validate_repo(repo)
        self.opener = opener or urlopen

    def _open(self, url):
        return self.opener(Request(url, headers={
            'User-Agent': 'proxys22-manager', 'Accept': 'application/vnd.github+json',
        }), timeout=30)

    def _json(self, url, limit):
        with self._open(url) as response:
            body = response.read(limit + 1)
        if len(body) > limit:
            raise ReleaseError('Release metadata is too large.')
        try:
            return json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise ReleaseError('Invalid release metadata.') from exc

    def load(self, tag=None):
        if tag is not None and not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
            raise ReleaseError('Release tag must use vMAJOR.MINOR.PATCH.')
        endpoint = 'latest' if tag is None else 'tags/' + quote(tag, safe='')
        try:
            release = self._json(f'https://api.github.com/repos/{self.repo}/releases/{endpoint}', 2 * 1024 * 1024)
            if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
                raise ReleaseError('A published stable release is required.')
            release_tag = release.get('tag_name', '')
            if not isinstance(release_tag, str) or not re.fullmatch(r'v\d+\.\d+\.\d+', release_tag):
                raise ReleaseError('Unexpected release tag.')
            if tag is not None and release_tag != tag:
                raise ReleaseError('GitHub returned a different release than the requested tag.')
            assets = release.get('assets', [])
            if not isinstance(assets, list):
                raise ReleaseError('Release assets are missing.')
            matches = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == MANIFEST_NAME]
            if len(matches) != 1:
                raise ReleaseError('The release does not contain proxys22-release.json.')
            url = asset_url(matches[0], self.repo, release_tag, MANIFEST_NAME)
            return parse_manifest(self._json(url, 65536), release_tag, assets, self.repo)
        except OSError as exc:
            raise ReleaseError('Cannot download the public GitHub release. Check Internet access and API rate limits.') from exc

    def download(self, release, kind, directory):
        if kind not in ('factory', 'sysupgrade'):
            raise ReleaseError('Image type must be factory or sysupgrade.')
        image = release.images[kind]
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / image.name
        if destination.exists():
            verify_image(destination, image)
            return destination
        descriptor, temporary = tempfile.mkstemp(prefix='.firmware-', suffix='.part', dir=directory)
        try:
            with os.fdopen(descriptor, 'wb') as output, self._open(image.url) as response:
                count = 0
                while block := response.read(65536):
                    count += len(block)
                    if count > image.size:
                        raise ReleaseError('Downloaded firmware exceeds the manifest size.')
                    output.write(block)
            verify_image(temporary, image)
            os.replace(temporary, destination)
        except OSError as exc:
            raise ReleaseError('Firmware download failed; no unverified image was installed.') from exc
        finally:
            Path(temporary).unlink(missing_ok=True)
        return destination
