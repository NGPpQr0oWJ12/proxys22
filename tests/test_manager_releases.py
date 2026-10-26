"""Release metadata and verified downloads, without hardware or network access."""

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from proxys22_manager.releases import (
    DEFAULT_REPO,
    Catalog,
    FirmwareImage,
    ReleaseError,
    parse_manifest,
    verify_image,
)


VERSION = "0.4.0"
TAG = "v" + VERSION
PAYLOAD = b"verified firmware fixture\x00\xff"


def release_fixture():
    images = {}
    assets = []
    for kind in ("factory", "sysupgrade"):
        name = f"proxys22-{VERSION}-keenetic_kn-1112-{kind}.bin"
        images[kind] = {
            "name": name,
            "size": len(PAYLOAD),
            "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
        }
        assets.append({
            "name": name,
            "size": len(PAYLOAD),
            "browser_download_url": (
                f"https://github.com/{DEFAULT_REPO}/releases/download/{TAG}/{name}"
            ),
        })
    manifest = {
        "schema": 1,
        "brand": "proxys22",
        "version": VERSION,
        "profile": "keenetic_kn-1112",
        "target": "ramips/mt76x8",
        "images": images,
    }
    return manifest, assets


class Response(io.BytesIO):
    def __init__(self, payload, on_read=None):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}
        self.status = 200
        self.on_read = on_read

    def read(self, size=-1):
        if self.on_read:
            self.on_read()
        return super().read(size)


class FakeOpener:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, request, *args, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"Unexpected network request: {url}")
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else Response(value)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.assets = release_fixture()

    def parse(self, manifest=None, assets=None, tag=TAG):
        return parse_manifest(
            self.manifest if manifest is None else manifest,
            tag,
            self.assets if assets is None else assets,
        )

    def test_valid_release_maps_each_image_to_its_github_asset(self):
        release = self.parse()
        self.assertEqual((release.version, release.tag), (VERSION, TAG))
        self.assertEqual(set(release.images), {"factory", "sysupgrade"})
        for kind, image in release.images.items():
            with self.subTest(kind=kind):
                self.assertIsInstance(image, FirmwareImage)
                self.assertEqual(image.kind, kind)
                self.assertEqual(image.name, self.manifest["images"][kind]["name"])
                self.assertEqual(image.size, len(PAYLOAD))
                self.assertEqual(image.sha256, hashlib.sha256(PAYLOAD).hexdigest())
                self.assertEqual(
                    image.url,
                    f"https://github.com/{DEFAULT_REPO}/releases/download/{TAG}/{image.name}",
                )

    def test_required_image_and_matching_asset_are_both_required(self):
        for kind in ("factory", "sysupgrade"):
            with self.subTest(kind=kind, missing="manifest image"):
                manifest = copy.deepcopy(self.manifest)
                del manifest["images"][kind]
                with self.assertRaises(ReleaseError):
                    self.parse(manifest=manifest)
            with self.subTest(kind=kind, missing="github asset"):
                name = self.manifest["images"][kind]["name"]
                with self.assertRaises(ReleaseError):
                    self.parse(assets=[item for item in self.assets if item["name"] != name])

    def test_wrong_schema_brand_board_target_and_version_are_rejected(self):
        for key, value in (
            ("schema", 2),
            ("brand", "other"),
            ("profile", "keenetic_kn-1110"),
            ("target", "ramips/mt7621"),
            ("version", "0.4.1"),
            ("version", "../../0.4.0"),
        ):
            with self.subTest(key=key, value=value):
                manifest = copy.deepcopy(self.manifest)
                manifest[key] = value
                with self.assertRaises(ReleaseError):
                    self.parse(manifest=manifest)
        for tag in (VERSION, "v0.4.1", "../v0.4.0"):
            with self.subTest(tag=tag), self.assertRaises(ReleaseError):
                self.parse(tag=tag)

    def test_size_must_be_a_positive_integer_inside_flash_limit(self):
        for value in (True, False, 0, -1, 16121857, 1.5, "25"):
            with self.subTest(value=value):
                manifest = copy.deepcopy(self.manifest)
                manifest["images"]["factory"]["size"] = value
                with self.assertRaises(ReleaseError):
                    self.parse(manifest=manifest)

    def test_sha256_must_be_lowercase_hex_of_exact_length(self):
        for value in ("", "a" * 63, "a" * 65, "g" * 64, "A" * 64, None):
            with self.subTest(value=value):
                manifest = copy.deepcopy(self.manifest)
                manifest["images"]["factory"]["sha256"] = value
                with self.assertRaises(ReleaseError):
                    self.parse(manifest=manifest)

    def test_image_names_cannot_traverse_or_change_board_or_kind(self):
        original = self.manifest["images"]["factory"]["name"]
        for name in (
            "../" + original,
            "folder/" + original,
            "folder\\" + original,
            original.replace("kn-1112", "kn-1110"),
            original.replace("factory", "sysupgrade"),
        ):
            with self.subTest(name=name):
                manifest = copy.deepcopy(self.manifest)
                assets = copy.deepcopy(self.assets)
                manifest["images"]["factory"]["name"] = name
                assets[0]["name"] = name
                assets[0]["browser_download_url"] = (
                    f"https://github.com/{DEFAULT_REPO}/releases/download/{TAG}/{name}"
                )
                with self.assertRaises(ReleaseError):
                    self.parse(manifest=manifest, assets=assets)

    def test_assets_must_have_exact_repository_tag_filename_and_https_url(self):
        original = self.assets[0]["browser_download_url"]
        for url in (
            original.replace("https://", "http://"),
            original.replace("github.com/", "github.com.evil.example/"),
            original.replace(DEFAULT_REPO, "other-owner/other-repo"),
            original.replace(TAG, "v0.4.1"),
            original + "?download=1",
            original + "#fragment",
        ):
            with self.subTest(url=url):
                assets = copy.deepcopy(self.assets)
                assets[0]["browser_download_url"] = url
                with self.assertRaises(ReleaseError):
                    self.parse(assets=assets)

    def test_github_asset_size_must_match_manifest(self):
        assets = copy.deepcopy(self.assets)
        assets[0]["size"] += 1
        with self.assertRaises(ReleaseError):
            self.parse(assets=assets)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        manifest, assets = release_fixture()
        self.image = parse_manifest(manifest, TAG, assets).images["factory"]

    def test_exact_bytes_are_accepted_and_file_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / self.image.name
            path.write_bytes(PAYLOAD)
            verify_image(path, self.image)
            self.assertEqual(path.read_bytes(), PAYLOAD)

    def test_wrong_size_and_same_size_corruption_are_rejected(self):
        for payload in (PAYLOAD[:-1], b"x" * len(PAYLOAD)):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / self.image.name
                path.write_bytes(payload)
                with self.assertRaises(ReleaseError):
                    verify_image(path, self.image)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.assets = release_fixture()
        self.manifest_url = (
            f"https://github.com/{DEFAULT_REPO}/releases/download/{TAG}/proxys22-release.json"
        )
        self.metadata = {
            "tag_name": TAG,
            "draft": False,
            "prerelease": False,
            "assets": self.assets + [{
                "name": "proxys22-release.json",
                "browser_download_url": self.manifest_url,
                "size": len(json.dumps(self.manifest).encode()),
            }],
        }

    def opener(self, metadata=None, endpoint="latest", manifest=None):
        api_url = f"https://api.github.com/repos/{DEFAULT_REPO}/releases/{endpoint}"
        return FakeOpener({
            api_url: json.dumps(self.metadata if metadata is None else metadata).encode(),
            self.manifest_url: json.dumps(self.manifest if manifest is None else manifest).encode(),
        })

    def test_latest_and_pinned_release_load_the_checked_manifest(self):
        for tag, endpoint in ((None, "latest"), (TAG, "tags/" + TAG)):
            with self.subTest(tag=tag):
                opener = self.opener(endpoint=endpoint)
                release = Catalog(opener=opener).load(tag)
                self.assertEqual((release.version, release.tag), (VERSION, TAG))
                self.assertEqual(set(release.images), {"factory", "sysupgrade"})
                self.assertEqual(opener.calls, [
                    f"https://api.github.com/repos/{DEFAULT_REPO}/releases/{endpoint}",
                    self.manifest_url,
                ])

    def test_draft_and_prerelease_are_refused_before_manifest_download(self):
        for field in ("draft", "prerelease"):
            with self.subTest(field=field):
                metadata = copy.deepcopy(self.metadata)
                metadata[field] = True
                opener = self.opener(metadata=metadata)
                with self.assertRaises(ReleaseError):
                    Catalog(opener=opener).load()
                self.assertEqual(len(opener.calls), 1)

    def test_explicit_tag_must_equal_release_returned_by_api(self):
        opener = self.opener(endpoint="tags/v0.3.0")
        with self.assertRaises(ReleaseError):
            Catalog(opener=opener).load("v0.3.0")

    def test_manifest_asset_is_required_unique_and_repository_scoped(self):
        mutations = (
            lambda data: data["assets"].pop(),
            lambda data: data["assets"].append(copy.deepcopy(data["assets"][-1])),
            lambda data: data["assets"][-1].update({
                "browser_download_url": "https://example.invalid/proxys22-release.json",
            }),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                metadata = copy.deepcopy(self.metadata)
                mutate(metadata)
                opener = self.opener(metadata=metadata)
                with self.assertRaises(ReleaseError):
                    Catalog(opener=opener).load()
                self.assertEqual(len(opener.calls), 1)

    def test_invalid_tag_is_refused_without_network_access(self):
        opener = FakeOpener({})
        for tag in (VERSION, "../latest", "v0.4.0?x=1"):
            with self.subTest(tag=tag), self.assertRaises(ReleaseError):
                Catalog(opener=opener).load(tag)
        self.assertEqual(opener.calls, [])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        manifest, assets = release_fixture()
        self.release = parse_manifest(manifest, TAG, assets)
        self.image = self.release.images["factory"]

    def test_download_publishes_verified_final_file_atomically(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            final = directory / self.image.name
            observed = []

            def during_read():
                observed.append(final.exists())

            opener = FakeOpener({
                self.image.url: lambda: Response(PAYLOAD, on_read=during_read),
            })
            result = Catalog(opener=opener).download(self.release, "factory", directory)
            self.assertEqual(result, final)
            self.assertTrue(observed)
            self.assertFalse(any(observed), "Final image appeared before verification finished")
            self.assertEqual(final.read_bytes(), PAYLOAD)
            self.assertEqual(set(directory.iterdir()), {final})
            self.assertEqual(opener.calls, [self.image.url])

    def test_bad_hash_and_truncated_download_leave_no_artifacts(self):
        for payload in (PAYLOAD[:-1], b"x" * len(PAYLOAD), PAYLOAD + b"oversized"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                opener = FakeOpener({self.image.url: payload})
                with self.assertRaises(ReleaseError):
                    Catalog(opener=opener).download(self.release, "factory", directory)
                self.assertEqual(list(directory.iterdir()), [])

    def test_interrupted_download_cleans_up_temporary_file(self):
        class InterruptedResponse(Response):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("Connection interrupted")
                return super().read(5)

        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            opener = FakeOpener({self.image.url: lambda: InterruptedResponse(PAYLOAD)})
            with self.assertRaises(ReleaseError):
                Catalog(opener=opener).download(self.release, "factory", directory)
            self.assertEqual(list(directory.iterdir()), [])

    def test_verified_cached_image_is_reused_without_network_access(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            final = directory / self.image.name
            final.write_bytes(PAYLOAD)
            opener = FakeOpener({})
            result = Catalog(opener=opener).download(self.release, "factory", directory)
            self.assertEqual(result, final)
            self.assertEqual(final.read_bytes(), PAYLOAD)
            self.assertEqual(opener.calls, [])

    def test_unknown_image_kind_cannot_make_a_network_request(self):
        with tempfile.TemporaryDirectory() as temp:
            opener = FakeOpener({})
            with self.assertRaises(ReleaseError):
                Catalog(opener=opener).download(self.release, "bootloader", Path(temp))
            self.assertEqual(opener.calls, [])


if __name__ == "__main__":
    unittest.main()
