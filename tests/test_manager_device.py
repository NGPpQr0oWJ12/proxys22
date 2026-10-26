"""Device upgrade safeguards exercised with fake SSH and in-memory backups."""

import copy
import hashlib
import io
from pathlib import Path
import shlex
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from proxys22_manager.device import DeviceError, Router, validate_board, verify_backup
from proxys22_manager.releases import FirmwareImage, ReleaseError


PAYLOAD = b"firmware fixture\x00\xff"
REQUIRED = (
    "/etc/config/network",
    "/etc/config/wireless",
    "/etc/wifi-proxy/initialized",
    "/etc/wifi-proxy/client.json",
    "/etc/wifi-proxy/awg.json",
)
BOARD = {
    "board_name": "keenetic,kn-1112",
    "model": "Keenetic Start KN-1112",
    "release": {"target": "ramips/mt76x8"},
}


def backup_archive(names=REQUIRED, prefix=""):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in names:
            body = b"" if path.endswith("/initialized") else b"fixture backup contents\n"
            member = tarfile.TarInfo(prefix + path.lstrip("/"))
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


class FakeRouter(Router):
    """Run actual backup/upgrade orchestration without creating an SSH client."""

    def __init__(self):
        self.events = []
        self.commands = []
        self.board_data = copy.deepcopy(BOARD)
        self.existing_files = list(REQUIRED)
        self.archive = backup_archive()
        self.remote_digest = hashlib.sha256(PAYLOAD).hexdigest()
        self.remote_hash_response = None
        self.compatibility_error = False
        self.upload_error = False
        self.saved_backup = None
        self.uploaded = None

    def board(self):
        self.events.append("board")
        validate_board(self.board_data)
        return self.board_data

    def firmware(self):
        self.events.append("firmware")
        return {"name": "proxys22", "version": "0.3.0"}

    def backup(self, directory):
        result = super().backup(directory)
        self.saved_backup = result
        self.events.append("backup-verified")
        return result

    def run(self, command, data=None, timeout=30, limit=2 * 1024 * 1024):
        self.commands.append(command)
        if command.startswith("for f in "):
            self.events.append("existing-files")
            return ("\n".join(self.existing_files) + "\n").encode()
        if "sysupgrade -b " in command:
            self.events.append("backup-command")
            return self.archive
        if command.startswith("umask 077; cat > "):
            self.events.append("upload")
            self.uploaded = data
            if self.upload_error:
                raise DeviceError("Fixture upload failed")
            return b""
        if command.startswith("sha256sum "):
            self.events.append("remote-hash")
            if self.remote_hash_response is not None:
                return self.remote_hash_response
            return (self.remote_digest + "  firmware.bin\n").encode()
        if command.startswith("sysupgrade -T "):
            self.events.append("compatibility")
            if self.compatibility_error:
                raise DeviceError("Fixture image is incompatible")
            return b""
        if "sysupgrade " in command and "PROXYS22_STARTED" in command:
            self.events.append("flash")
            return b"PROXYS22_STARTED\n"
        if command.startswith("rm -f "):
            self.events.append("cleanup")
            return b""
        raise AssertionError(f"Unexpected device command: {command}")


class BoardTests(unittest.TestCase):
    def test_exact_model_and_target_are_accepted(self):
        validate_board(copy.deepcopy(BOARD))

    def test_wrong_or_unidentified_model_is_rejected(self):
        for board in (None, {}, {"board_name": "keenetic,kn-1110"}):
            with self.subTest(board=board), self.assertRaises(DeviceError):
                validate_board(board)

    def test_wrong_or_missing_target_is_rejected(self):
        for release in ({}, {"target": "ramips/mt7621"}):
            board = copy.deepcopy(BOARD)
            board["release"] = release
            with self.subTest(release=release), self.assertRaises(DeviceError):
                validate_board(board)


class BackupTests(unittest.TestCase):
    def test_complete_archive_accepts_normal_and_dot_prefixed_paths(self):
        for prefix in ("", "./"):
            with self.subTest(prefix=prefix):
                verify_backup(backup_archive(prefix=prefix), REQUIRED)

    def test_every_existing_setting_profile_and_marker_must_be_present(self):
        for omitted in REQUIRED:
            names = [path for path in REQUIRED if path != omitted]
            with self.subTest(omitted=omitted), self.assertRaises(DeviceError):
                verify_backup(backup_archive(names), REQUIRED)

    def test_network_is_required_even_if_remote_listing_omits_it(self):
        names = [path for path in REQUIRED if path != "/etc/config/network"]
        with self.assertRaises(DeviceError):
            verify_backup(backup_archive(names), names)

    def test_invalid_or_unreadable_archives_are_rejected(self):
        for body in (b"", b"not gzip", backup_archive()[:30]):
            with self.subTest(body=body), self.assertRaises(DeviceError):
                verify_backup(body, REQUIRED)

    def test_missing_gzip_trailer_and_corrupt_crc_are_rejected(self):
        archive = backup_archive()
        corrupt_crc = archive[:-8] + bytes([archive[-8] ^ 1]) + archive[-7:]
        for name, body in (("missing trailer", archive[:-8]), ("corrupt CRC", corrupt_crc)):
            with self.subTest(case=name), self.assertRaises(DeviceError):
                verify_backup(body, REQUIRED)

    def test_symlink_cannot_stand_in_for_existing_profile(self):
        output = io.BytesIO()
        names = [name for name in REQUIRED if not name.endswith("/awg.json")]
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for path in names:
                member = tarfile.TarInfo(path.lstrip("/"))
                archive.addfile(member, io.BytesIO(b""))
            member = tarfile.TarInfo("etc/wifi-proxy/awg.json")
            member.type = tarfile.SYMTYPE
            member.linkname = "client.json"
            archive.addfile(member)
        with self.assertRaises(DeviceError):
            verify_backup(output.getvalue(), REQUIRED)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "firmware.bin"
        self.path.write_bytes(PAYLOAD)
        self.image = FirmwareImage(
            "sysupgrade", "proxys22-0.4.0-keenetic_kn-1112-sysupgrade.bin",
            len(PAYLOAD), hashlib.sha256(PAYLOAD).hexdigest(),
            "https://example.invalid/fixture.bin",
        )
        self.router = FakeRouter()
        self.confirmed = False

    def confirm(self, prompt, expected):
        self.router.events.append("confirm")
        self.assertEqual(expected, "UPDATE")
        self.assertIn("UPDATE", prompt)
        self.assertIsNotNone(self.router.saved_backup)
        self.assertTrue(self.router.saved_backup.is_file())
        self.assertLess(self.router.events.index("backup-verified"), self.router.events.index("remote-hash"))
        self.assertLess(self.router.events.index("remote-hash"), self.router.events.index("compatibility"))
        self.assertLess(self.router.events.index("compatibility"), self.router.events.index("confirm"))
        self.confirmed = True
        return True

    def upgrade(self, confirm=None, image=None):
        with patch("builtins.print"):
            return self.router.upgrade(
                self.path, image or self.image, self.root / "backups", confirm or self.confirm,
            )

    def assert_no_flash(self):
        self.assertNotIn("flash", self.router.events)

    def assert_remote_cleanup(self):
        self.assertIn("cleanup", self.router.events)
        upload = next(command for command in self.router.commands if command.startswith("umask 077; cat > "))
        remote = shlex.split(upload.split("cat > ", 1)[1])[0]
        self.assertIn("rm -f " + remote, self.router.commands)

    def test_backup_and_checks_precede_confirmation_and_unforced_upgrade(self):
        result = self.upgrade()
        self.assertTrue(self.confirmed)
        self.assertEqual(result, self.router.saved_backup)
        self.assertEqual(self.router.uploaded, PAYLOAD)
        self.assertEqual(result.read_bytes(), self.router.archive)
        checksum = result.with_suffix(result.suffix + ".sha256").read_text()
        self.assertEqual(checksum, f"{hashlib.sha256(self.router.archive).hexdigest()}  {result.name}\n")
        self.assertLess(self.router.events.index("confirm"), self.router.events.index("flash"))
        self.assertNotIn("cleanup", self.router.events)
        for command in self.router.commands:
            tokens = shlex.split(command)
            self.assertNotIn("-F", tokens)
            self.assertNotIn("-n", tokens)

    def test_factory_image_is_rejected_before_device_access(self):
        image = FirmwareImage("factory", self.image.name, self.image.size, self.image.sha256, self.image.url)
        with self.assertRaises(DeviceError):
            self.upgrade(image=image)
        self.assertEqual(self.router.events, [])

    def test_wrong_local_hash_or_size_is_rejected_before_device_access(self):
        for payload in (PAYLOAD[:-1], b"x" * len(PAYLOAD)):
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                with self.assertRaises(ReleaseError):
                    self.upgrade()
                self.assertEqual(self.router.events, [])

    def test_wrong_device_model_or_target_prevents_backup_and_upload(self):
        for board in (
            {"board_name": "keenetic,kn-1110", "release": {"target": "ramips/mt76x8"}},
            {"board_name": "keenetic,kn-1112", "release": {"target": "ramips/mt7621"}},
        ):
            with self.subTest(board=board):
                self.router = FakeRouter()
                self.router.board_data = board
                with self.assertRaises(DeviceError):
                    self.upgrade()
                self.assertEqual(self.router.events, ["board"])

    def test_missing_initialization_marker_prevents_backup_upload_and_flash(self):
        self.router.existing_files.remove("/etc/wifi-proxy/initialized")
        with self.assertRaises(DeviceError):
            self.upgrade()
        self.assertNotIn("backup-command", self.router.events)
        self.assertNotIn("upload", self.router.events)
        self.assert_no_flash()
        self.assertFalse(self.confirmed)

    def test_incomplete_existing_profile_backup_prevents_upload_and_flash(self):
        self.router.archive = backup_archive([name for name in REQUIRED if not name.endswith("/awg.json")])
        with self.assertRaises(DeviceError):
            self.upgrade()
        self.assertNotIn("upload", self.router.events)
        self.assert_no_flash()
        self.assertFalse(self.confirmed)
        self.assertFalse((self.root / "backups").exists())

    def test_invalid_backup_prevents_upload_and_flash(self):
        self.router.archive = b"not a backup archive"
        with self.assertRaises(DeviceError):
            self.upgrade()
        self.assertNotIn("upload", self.router.events)
        self.assert_no_flash()
        self.assertFalse(self.confirmed)

    def test_remote_hash_mismatch_cleans_upload_without_confirming_or_flashing(self):
        self.router.remote_digest = "0" * 64
        with self.assertRaises(DeviceError):
            self.upgrade()
        self.assert_remote_cleanup()
        self.assertNotIn("compatibility", self.router.events)
        self.assert_no_flash()
        self.assertFalse(self.confirmed)
        self.assertTrue(self.router.saved_backup.is_file())

    def test_empty_remote_hash_is_a_verification_error_and_cleans_upload(self):
        self.router.remote_hash_response = b""
        with self.assertRaises(DeviceError):
            self.upgrade()
        self.assert_remote_cleanup()
        self.assert_no_flash()
        self.assertFalse(self.confirmed)

    def test_failed_compatibility_check_cleans_upload_without_confirmation(self):
        self.router.compatibility_error = True
        with self.assertRaises(DeviceError):
            self.upgrade()
        self.assert_remote_cleanup()
        self.assert_no_flash()
        self.assertFalse(self.confirmed)

    def test_cancellation_keeps_backup_cleans_upload_and_never_flashes(self):
        def cancel(prompt, expected):
            self.confirm(prompt, expected)
            return False

        with self.assertRaises(DeviceError):
            self.upgrade(confirm=cancel)
        self.assertTrue(self.confirmed)
        self.assert_remote_cleanup()
        self.assert_no_flash()
        self.assertTrue(self.router.saved_backup.is_file())

    def test_failed_upload_cleans_partial_remote_file_without_flashing(self):
        self.router.upload_error = True
        with self.assertRaises(DeviceError):
            self.upgrade()
        self.assert_remote_cleanup()
        self.assertNotIn("remote-hash", self.router.events)
        self.assert_no_flash()
        self.assertFalse(self.confirmed)


if __name__ == "__main__":
    unittest.main()
