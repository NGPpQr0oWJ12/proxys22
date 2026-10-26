"""Protocol checks run exclusively on loopback with ephemeral UDP ports."""

import importlib.util
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "serve-recovery.py"
SPEC = importlib.util.spec_from_file_location("recovery_server", SCRIPT)
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


def request(filename=recovery.FILENAME, mode=b"octet", options=(), opcode=1):
    packet = struct.pack("!H", opcode) + filename + b"\0" + mode + b"\0"
    for key, value in options:
        packet += key + b"\0" + value + b"\0"
    return packet


def ack(block):
    return struct.pack("!HH", recovery.ACK, block)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "factory.bin"
        self.server = None
        self.worker = None
        self.clients = []
        self.errors = []

    def tearDown(self):
        if self.server:
            self.server.close()
            self.worker.join(2)
            self.assertFalse(self.worker.is_alive(), "Server did not stop")
        for client in self.clients:
            client.close()
        self.temp.cleanup()
        self.assertEqual(self.errors, [])

    def start(self, body, timeout=0.08, retries=2, progress=None):
        self.image.write_bytes(body)
        self.server = recovery.RecoveryServer(
            self.image, "127.0.0.1", port=0, timeout=timeout,
            retries=retries, allow_loopback=True, progress=progress)

        def run():
            try:
                self.server.serve_forever()
            except Exception as exc:
                self.errors.append(exc)

        self.worker = threading.Thread(target=run)
        self.worker.start()
        self.assertGreater(self.server.address[1], 1024)

    def client(self):
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        client.settimeout(1)
        self.clients.append(client)
        return client

    def download(self, options=()):
        client = self.client()
        client.sendto(request(options=options), self.server.address)
        received = bytearray()
        packets = []
        negotiated = {}
        peer = None
        block_size = 512
        expected = 1
        while True:
            packet, source = client.recvfrom(65536)
            if peer is None:
                peer = source
                self.assertNotEqual(peer[1], self.server.address[1])
            self.assertEqual(source, peer)
            opcode = struct.unpack("!H", packet[:2])[0]
            if opcode == recovery.OACK:
                fields = packet[2:-1].split(b"\0")
                negotiated = dict(zip(fields[::2], fields[1::2]))
                block_size = int(negotiated.get(b"blksize", b"512"))
                client.sendto(ack(0), peer)
                continue
            self.assertEqual(opcode, recovery.DATA)
            block = struct.unpack("!H", packet[2:4])[0]
            self.assertEqual(block, expected)
            packets.append(packet)
            received.extend(packet[4:])
            client.sendto(ack(block), peer)
            if len(packet) - 4 < block_size:
                return bytes(received), packets, negotiated
            expected = (expected + 1) & 0xffff

    def wait_idle(self):
        deadline = time.monotonic() + 1
        while self.server.transfer and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNone(self.server.transfer)

    def test_regular_transfer_is_exact(self):
        body = bytes(range(256)) * 5 + b"last"
        self.start(body)
        received, packets, options = self.download()
        self.assertEqual(received, body)
        self.assertEqual([len(packet) - 4 for packet in packets], [512, 512, 260])
        self.assertEqual(options, {})

    def test_progress_counts_only_acknowledged_bytes(self):
        updates = []
        completed = threading.Event()

        def progress(count, total):
            updates.append((count, total))
            if count == total:
                completed.set()

        self.start(b'x' * 1024, progress=progress)
        client = self.client()
        client.sendto(request(), self.server.address)
        _, peer = client.recvfrom(65536)
        self.assertEqual(updates, [(0, 1024)])
        client.sendto(ack(1), peer)
        client.recvfrom(65536)
        self.assertEqual(updates[-1], (512, 1024))
        client.sendto(ack(2), peer)
        final, _ = client.recvfrom(65536)
        self.assertEqual(len(final), 4)
        self.assertFalse(completed.is_set())
        client.sendto(ack(3), peer)
        self.assertTrue(completed.wait(1))
        self.assertEqual(updates[-1], (1024, 1024))

    def test_exact_multiple_requires_final_empty_block(self):
        body = b"x" * 1024
        self.start(body)
        received, packets, _ = self.download()
        self.assertEqual(received, body)
        self.assertEqual([len(packet) - 4 for packet in packets], [512, 512, 0])

    def test_option_negotiation_caps_block_size_and_reports_size(self):
        body = bytes(range(256)) * 12
        self.start(body)
        received, packets, options = self.download(
            ((b"blksize", b"8192"), (b"tsize", b"0")))
        self.assertEqual(received, body)
        self.assertEqual(options, {b"blksize": b"1468", b"tsize": b"3072"})
        self.assertTrue(all(len(packet) <= 1472 for packet in packets))

    def test_rejects_traversal_write_wrong_file_and_non_octet(self):
        self.start(b"firmware")
        client = self.client()
        for packet, code in (
            (request(filename=b"../KN-1112_recovery.bin"), 1),
            (request(filename=b"folder\\KN-1112_recovery.bin"), 1),
            (request(filename=b"other.bin"), 1),
            (request(opcode=recovery.WRQ), 4),
            (request(mode=b"netascii"), 4),
            (request()[:-1], 4),
            (request(options=((b"unknown", b"x" * 512),)), 4),
            (request(options=((b"blksize", b"0"),)), 8),
            (request(options=((b"tsize", b"1"),)), 8),
            (request(options=((b"tsize", b"0"), (b"TSIZE", b"0"))), 8),
        ):
            with self.subTest(packet=packet):
                client.sendto(packet, self.server.address)
                reply, _ = client.recvfrom(1024)
                self.assertEqual(struct.unpack("!HH", reply[:4]), (recovery.ERROR, code))
                self.assertIsNone(self.server.transfer)
        self.assertEqual(self.image.read_bytes(), b"firmware")

    def test_unrecognized_options_fall_back_to_normal_data(self):
        self.start(b"firmware")
        received, packets, options = self.download(((b"windowsize", b"4"),))
        self.assertEqual(received, b"firmware")
        self.assertEqual(options, {})
        self.assertEqual(len(packets), 1)

    def test_oack_is_retransmitted_until_ack_zero(self):
        self.start(b"firmware")
        client = self.client()
        client.sendto(request(options=((b"tsize", b"0"),)), self.server.address)
        first, peer = client.recvfrom(1024)
        resent, source = client.recvfrom(1024)
        self.assertEqual(first, struct.pack("!H", recovery.OACK) + b"tsize\x008\0")
        self.assertEqual((resent, source), (first, peer))
        client.sendto(ack(0), peer)
        data, source = client.recvfrom(1024)
        self.assertEqual(source, peer)
        self.assertEqual(data, struct.pack("!HH", recovery.DATA, 1) + b"firmware")
        client.sendto(ack(1), peer)
        self.wait_idle()

    def test_timeout_retransmits_same_packet_then_ends(self):
        self.start(b"firmware", retries=1)
        client = self.client()
        client.sendto(request(), self.server.address)
        first, peer = client.recvfrom(1024)
        resent, source = client.recvfrom(1024)
        self.assertEqual(resent, first)
        self.assertEqual(source, peer)
        self.wait_idle()
        self.assertEqual(self.image.read_bytes(), b"firmware")

    def test_wrong_peer_ack_and_wrong_block_cannot_advance(self):
        self.start(b"z" * 700)
        client, other = self.client(), self.client()
        client.sendto(request(), self.server.address)
        first, peer = client.recvfrom(1024)
        other.sendto(ack(1), peer)
        rejected, _ = other.recvfrom(1024)
        self.assertEqual(struct.unpack("!HH", rejected[:4]), (recovery.ERROR, 5))
        client.sendto(ack(2), peer)
        resent, _ = client.recvfrom(1024)
        self.assertEqual(resent, first)
        client.sendto(ack(1), peer)
        second, _ = client.recvfrom(1024)
        self.assertEqual(second, struct.pack("!HH", recovery.DATA, 2) + b"z" * 188)
        client.sendto(ack(2), peer)
        self.wait_idle()

    def test_duplicate_request_reuses_transfer_port(self):
        self.start(b"firmware")
        client = self.client()
        client.sendto(request(), self.server.address)
        first, peer = client.recvfrom(1024)
        client.sendto(request(), self.server.address)
        repeated, source = client.recvfrom(1024)
        self.assertEqual((repeated, source), (first, peer))
        client.sendto(ack(1), peer)
        self.wait_idle()

    def test_listener_rejects_second_peer_while_busy(self):
        self.start(b"firmware")
        first, second = self.client(), self.client()
        first.sendto(request(), self.server.address)
        _, peer = first.recvfrom(1024)
        second.sendto(request(), self.server.address)
        reply, _ = second.recvfrom(1024)
        self.assertEqual(struct.unpack("!HH", reply[:4]), (recovery.ERROR, 0))
        first.sendto(ack(1), peer)
        self.wait_idle()

    def test_cli_bind_rejects_wildcard_loopback_and_non_ipv4(self):
        for address in ("0.0.0.0", "127.0.0.1", "127.1.2.3", "::1", "localhost", "224.0.0.1"):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    recovery.validate_bind(address)
        self.assertEqual(recovery.validate_bind("192.168.1.2"), "192.168.1.2")


if __name__ == "__main__":
    unittest.main()
