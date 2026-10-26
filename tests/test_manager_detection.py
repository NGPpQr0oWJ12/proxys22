"""Offline model detection and CLI preflight checks; never connect to hardware."""

import copy
from contextlib import nullcontext, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from proxys22_manager import cli
from proxys22_manager.device import DeviceError, Router, identify_board
from proxys22_manager.releases import PROFILE, TARGET


BOARD = {
    "board_name": "keenetic,kn-1112",
    "model": "Keenetic Start",
    "system": "MediaTek MT7628AN",
    "release": {"target": TARGET, "version": "24.10.0"},
}


class IdentityTests(unittest.TestCase):
    def test_exact_hardware_selects_the_existing_profile(self):
        identity = identify_board(copy.deepcopy(BOARD))
        self.assertTrue(identity["supported"])
        self.assertEqual(identity["profile"], PROFILE)
        self.assertEqual(identity["model"], "Keenetic Start KN-1112")
        self.assertEqual(identity["target"], TARGET)

    def test_display_name_cannot_make_different_hardware_supported(self):
        for name in ("keenetic,kn-1110", "cudy,tr3000-v1", "KEENETIC,KN-1112",
                     "keenetic,kn-1112 ", "keenetic,kn-1112\x00"):
            board = copy.deepcopy(BOARD)
            board.update(board_name=name, model="Keenetic Start KN-1112")
            with self.subTest(board_name=name):
                identity = identify_board(board)
                self.assertFalse(identity["supported"])
                self.assertIsNone(identity["profile"])

    def test_missing_or_invalid_board_identifier_is_an_error(self):
        for board in (None, [], {}, {"board_name": None}, {"board_name": 1112},
                      {"board_name": ""}, {"board_name": " \t\n"}):
            with self.subTest(board=board), self.assertRaises(DeviceError):
                identify_board(board)

    def test_wrong_missing_or_malformed_release_cannot_select_an_image(self):
        for release in (None, [], "ramips/mt76x8", {}, {"target": None},
                        {"target": "ramips/mt7621"}, {"target": TARGET + " "}):
            board = copy.deepcopy(BOARD)
            board["release"] = release
            with self.subTest(release=release):
                identity = identify_board(board)
                self.assertFalse(identity["supported"])
                self.assertIsNone(identity["profile"])
        board.pop("release")
        self.assertFalse(identify_board(board)["supported"])

    def test_unknown_cudy_reports_hardware_without_claiming_support(self):
        board = {"board_name": "cudy,tr3000-v1", "model": "Cudy TR3000 v1",
                 "system": "MediaTek MT7981", "release": {"target": "mediatek/filogic"}}
        identity = identify_board(board)
        self.assertEqual(identity["model"], "Cudy TR3000 v1")
        self.assertEqual(identity["board_name"], "cudy,tr3000-v1")
        self.assertEqual(identity["target"], "mediatek/filogic")
        self.assertFalse(identity["supported"])
        self.assertIsNone(identity["profile"])

    def test_identity_is_bounded_printable_and_excludes_private_fields(self):
        secret = "fixture-private-value"
        board = {"board_name": "cudy,tr3000-v1\x00", "model": "Cudy\n\x1b[31m" + "x" * 300,
                 "system": "cpu\r\t", "hostname": secret, "password": secret,
                 "private_key": secret, "release": {"target": "mediatek/filogic\n",
                 "version": "24.10\x07", "token": secret}}
        identity = identify_board(board)
        self.assertNotIn(secret, json.dumps(identity))
        for value in identity.values():
            if isinstance(value, str):
                self.assertTrue(value.isprintable())
                self.assertLessEqual(len(value), 160)


class RouterDetectionTests(unittest.TestCase):
    def test_bare_openwrt_detection_needs_only_read_only_board_rpc(self):
        device = Router("fixture.invalid")
        device.run = Mock(return_value=json.dumps(BOARD).encode("utf-8"))
        self.assertTrue(device.identify()["supported"])
        device.run.assert_called_once_with("ubus call system board")

    def test_invalid_board_rpc_payloads_fail_closed(self):
        for response in (b"not json", b"[]", b"null", b"{}"):
            device = Router("fixture.invalid")
            device.run = Mock(return_value=response)
            with self.subTest(response=response), self.assertRaises(DeviceError):
                device.identify()
            device.run.assert_called_once_with("ubus call system board")

    def test_existing_board_method_still_rejects_unsupported_hardware(self):
        device = Router("fixture.invalid")
        board = dict(BOARD, board_name="cudy,tr3000-v1")
        device.run = Mock(return_value=json.dumps(board).encode("utf-8"))
        self.assertFalse(device.identify()["supported"])
        with self.assertRaises(DeviceError):
            device.board()


class DetectionCliTests(unittest.TestCase):
    def setUp(self):
        self.console = Mock()
        self.addCleanup(patch.stopall)
        patch.object(cli, "console", self.console).start()
        self.prepare = patch.object(cli, "prepare_image").start()
        self.download = patch.object(cli, "download_image").start()
        self.catalog = patch.object(cli, "get_release").start()
        self.recovery = Mock()
        self.recovery.LOG.level, self.recovery.LOG.propagate = 30, False
        patch.object(cli, "load_recovery", return_value=self.recovery).start()

    def connect(self, board=BOARD, firmware=None, error=None, events=None):
        device = Router("fixture.invalid")

        def read(command, *args, **kwargs):
            if error:
                raise error
            if command == "ubus call system board":
                if events is not None:
                    events.append("identify")
                return json.dumps(board).encode("utf-8")
            if command == "cat /etc/proxys22-release":
                if events is not None:
                    events.append("firmware")
                return json.dumps(firmware).encode("utf-8")
            raise AssertionError("Unexpected device side effect: " + command)

        device.run = Mock(side_effect=read)
        device.backup, device.upgrade = Mock(), Mock()
        self.router = patch.object(cli, "router", return_value=nullcontext(device)).start()
        return device

    def assert_no_image_or_write(self, device):
        for operation in (self.prepare, self.download, self.catalog, device.backup,
                          device.upgrade, self.recovery.RecoveryServer):
            operation.assert_not_called()

    def test_wrong_device_stops_update_before_image_or_write_work(self):
        board = dict(BOARD, board_name="cudy,tr3000-v1", model="Keenetic Start KN-1112")
        device = self.connect(board=board)
        with self.assertRaises(DeviceError):
            cli.run_command(cli.parser().parse_args(["update", "--host", "fixture.invalid"]))
        self.assert_no_image_or_write(device)
        device.run.assert_called_once_with("ubus call system board")

    def test_failed_detection_stops_update_before_image_or_write_work(self):
        device = self.connect(error=DeviceError("fixture identification failure"))
        with self.assertRaises(DeviceError):
            cli.run_command(cli.parser().parse_args(["update", "--host", "fixture.invalid"]))
        self.assert_no_image_or_write(device)

    def test_stock_openwrt_cannot_download_or_start_an_update(self):
        device = self.connect(firmware={"name": "OpenWrt", "version": "24.10.0"})
        with self.assertRaises(DeviceError):
            cli.run_command(cli.parser().parse_args(["update", "--host", "fixture.invalid"]))
        self.assert_no_image_or_write(device)
        self.assertEqual([call.args[0] for call in device.run.call_args_list],
                         ["ubus call system board", "cat /etc/proxys22-release"])

    def test_supported_update_checks_hardware_and_firmware_before_image(self):
        events = []
        device = self.connect(firmware={"name": "proxys22", "version": "0.4.0"}, events=events)
        image, path = object(), Path("fixture-sysupgrade.bin")
        release = SimpleNamespace(version="0.4.1", images={"sysupgrade": image})
        self.prepare.side_effect = lambda *args: (events.append("prepare") or (release, path))
        device.upgrade.side_effect = lambda *args: events.append("upgrade")
        args = cli.parser().parse_args(["update", "--host", "fixture.invalid"])
        cli.run_command(args)
        self.assertEqual(events, ["identify", "firmware", "prepare", "upgrade"])
        self.prepare.assert_called_once_with(args, "sysupgrade")
        device.upgrade.assert_called_once_with(path, image, args.state_dir / "backups", cli.confirm)

    def test_flash_host_mismatch_cannot_prepare_image_or_start_recovery(self):
        device = self.connect(board=dict(BOARD, board_name="cudy,tr3000-v1"))
        args = cli.parser().parse_args(["flash", "--bind", "192.168.1.2", "--host", "fixture.invalid"])
        with self.assertRaises(DeviceError):
            cli.run_command(args)
        self.assert_no_image_or_write(device)

    def test_failed_flash_detection_cannot_start_recovery(self):
        device = self.connect(error=DeviceError("fixture detection failure"))
        args = cli.parser().parse_args(["flash", "--bind", "192.168.1.2", "--host", "fixture.invalid"])
        with self.assertRaises(DeviceError):
            cli.run_command(args)
        self.assert_no_image_or_write(device)

    def test_supported_flash_checks_hardware_before_serving_verified_image(self):
        events = []
        device = self.connect(events=events)
        body = b"fixture factory image"
        image = SimpleNamespace(size=len(body), sha256=hashlib.sha256(body).hexdigest())
        release = SimpleNamespace(images={"factory": image})
        self.prepare.side_effect = lambda *args: (events.append("prepare") or (release, Path("factory.bin")))
        server = Mock(image=body)
        self.recovery.RecoveryServer.side_effect = lambda *args, **kwargs: (events.append("serve") or server)
        args = cli.parser().parse_args(["flash", "--bind", "192.168.1.2", "--host", "fixture.invalid"])
        with patch.object(cli, "confirm", return_value=True) as confirm:
            cli.run_command(args)
        self.assertEqual(events, ["identify", "prepare", "serve"])
        confirm.assert_called_once()
        device.run.assert_called_once_with("ubus call system board")
        server.serve_forever.assert_called_once_with()
        server.close.assert_called_once_with()

    def test_recovery_only_flash_does_not_attempt_ssh(self):
        device = self.connect()
        self.prepare.return_value = (Mock(), Path("factory.bin"))
        args = cli.parser().parse_args(["flash", "--bind", "192.168.1.2"])
        with patch.object(cli, "confirm", return_value=False), self.assertRaises(DeviceError):
            cli.run_command(args)
        self.router.assert_not_called()
        device.run.assert_not_called()
        self.prepare.assert_called_once_with(args, "factory")
        self.recovery.RecoveryServer.assert_not_called()

    def test_detect_json_main_returns_clean_report_without_firmware_or_images(self):
        board = dict(BOARD, board_name="cudy,tr3000-v1", model="Cudy TR3000 v1")
        device = self.connect(board=board)
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main(["detect", "--host", "fixture.invalid", "--json"])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["model"], "Cudy TR3000 v1")
        self.assertFalse(report["supported"])
        self.assertNotIn("\x1b", output.getvalue())
        device.run.assert_called_once_with("ubus call system board")
        self.assert_no_image_or_write(device)

    def test_menu_eight_routes_to_read_only_detection(self):
        self.console.prompt.side_effect = ["8", "fixture.invalid", "", "0"]
        with patch.object(cli, "run_command") as run:
            cli.menu(cli.parser().parse_args([]))
        run.assert_called_once()
        selected = run.call_args.args[0]
        self.assertEqual((selected.command, selected.host, selected.ssh_port),
                         ("detect", "fixture.invalid", 22))
        self.assertFalse(selected.json)


if __name__ == "__main__":
    unittest.main()
