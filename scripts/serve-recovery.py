#!/usr/bin/env python3
"""Serve one verified KN-1112 factory image through read-only TFTP."""

import argparse
import ipaddress
import logging
from pathlib import Path
import select
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


FILENAME = b"KN-1112_recovery.bin"
DEFAULT_BLOCK_SIZE = 512
MAX_BLOCK_SIZE = 1468
RRQ, WRQ, DATA, ACK, ERROR, OACK = 1, 2, 3, 4, 5, 6
LOG = logging.getLogger("recovery")


class RequestError(ValueError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def validate_bind(value: str, allow_loopback: bool = False) -> str:
    """Require an explicit unicast IPv4 interface; CLI never permits loopback."""
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError("Параметр --bind должен содержать IPv4-адрес") from exc
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        raise ValueError("Укажите конкретный IPv4-адрес проводного адаптера в --bind")
    if address.is_loopback and not allow_loopback:
        raise ValueError("Для --bind нужен адрес проводного адаптера; локальная петля не подходит")
    return str(address)


def parse_request(packet: bytes, image_size: int) -> Tuple[int, Dict[str, str]]:
    if len(packet) < 4 or len(packet) > 512:
        raise RequestError(4, "Invalid request")
    opcode = struct.unpack("!H", packet[:2])[0]
    if opcode == WRQ:
        raise RequestError(4, "Read-only server")
    if opcode != RRQ:
        raise RequestError(4, "Only read requests are supported")
    fields = packet[2:].split(b"\0")
    if fields[-1] != b"" or len(fields) < 3 or len(fields) % 2 != 1:
        raise RequestError(4, "Invalid request fields")
    if fields[0] != FILENAME:
        raise RequestError(1, "File not found")
    if fields[1].lower() != b"octet":
        raise RequestError(4, "Only octet mode is supported")

    block_size = DEFAULT_BLOCK_SIZE
    options = {}
    seen = set()
    for index in range(2, len(fields) - 1, 2):
        try:
            name = fields[index].decode("ascii").lower()
            value = fields[index + 1].decode("ascii")
        except UnicodeDecodeError as exc:
            raise RequestError(8, "Invalid option") from exc
        if not name or not value or name in seen:
            raise RequestError(8, "Invalid or duplicate option")
        seen.add(name)
        if name not in ("blksize", "tsize"):
            continue  # RFC 2347: unsupported options are ignored.
        if not value.isdecimal() or len(value) > 10:
            raise RequestError(8, "Invalid option value")
        number = int(value)
        if name == "blksize":
            if number < 8 or number > 65464:
                raise RequestError(8, "Invalid block size")
            block_size = min(number, MAX_BLOCK_SIZE)
            options[name] = str(block_size)
        elif number != 0:
            raise RequestError(8, "Read request tsize must be zero")
        else:
            options[name] = str(image_size)
    return block_size, options


def error_packet(code: int, message: str) -> bytes:
    return struct.pack("!HH", ERROR, code) + message.encode("ascii") + b"\0"


@dataclass
class Transfer:
    sock: socket.socket
    peer: Tuple[str, int]
    request: bytes
    block_size: int
    block: int
    packet: bytes
    deadline: float
    offset: int = 0
    payload_size: int = 0
    retries: int = 0
    negotiating: bool = False
    reported: int = 0


class RecoveryServer:
    """One active download, with all protocol state owned by serve_forever()."""

    def __init__(self, image: Path, bind: str, port: int = 69,
                 timeout: float = 3.0, retries: int = 5,
                 allow_loopback: bool = False, progress=None):
        self.bind = validate_bind(bind, allow_loopback)
        if not 0 <= port <= 65535 or timeout <= 0 or retries < 0:
            raise ValueError("Некорректный порт, время ожидания или число повторов")
        # An immutable snapshot prevents a rebuild from changing an active transfer.
        self.image = Path(image).read_bytes()
        if not self.image:
            raise ValueError("Файл прошивки пуст")
        self.progress = progress
        self.timeout = timeout
        self.max_retries = retries
        self.stop_event = threading.Event()
        self.transfer: Optional[Transfer] = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((self.bind, port))
            self.sock.setblocking(False)
        except Exception:
            self.sock.close()
            raise
        self.address = self.sock.getsockname()

    def close(self) -> None:
        """Request shutdown; the event loop closes its sockets within 0.2 seconds."""
        self.stop_event.set()

    @staticmethod
    def _error(sock: socket.socket, peer: Tuple[str, int],
               code: int, message: str) -> None:
        try:
            sock.sendto(error_packet(code, message), peer)
        except OSError:
            pass

    def _finish(self, message: str) -> None:
        if self.transfer:
            LOG.info("%s: подтверждено %d байт", message, self.transfer.offset)
            self.transfer.sock.close()
            self.transfer = None

    def _send(self, packet: bytes) -> None:
        transfer = self.transfer
        transfer.packet = packet
        transfer.retries = 0
        transfer.deadline = time.monotonic() + self.timeout
        transfer.sock.sendto(packet, transfer.peer)

    def _data(self) -> None:
        transfer = self.transfer
        payload = self.image[transfer.offset:transfer.offset + transfer.block_size]
        transfer.payload_size = len(payload)
        self._send(struct.pack("!HH", DATA, transfer.block) + payload)

    def _request(self) -> None:
        # Read a whole UDP datagram: Windows raises WSAEMSGSIZE on truncation.
        packet, peer = self.sock.recvfrom(65535)
        try:
            block_size, options = parse_request(packet, len(self.image))
        except RequestError as exc:
            if exc.code == 1:
                LOG.warning('Имя образа в запросе загрузчика не соответствует KN-1112. Передача прошивки запрещена.')
            self._error(self.sock, peer, exc.code, str(exc))
            return
        if self.transfer:
            if peer == self.transfer.peer and packet == self.transfer.request:
                self.transfer.sock.sendto(self.transfer.packet, peer)
            else:
                self._error(self.sock, peer, 0, "A transfer is already active")
            return

        LOG.info('Загрузчик запросил KN-1112_recovery.bin: выбран профиль Keenetic KN-1112. Аппаратную ревизию проверяют по наклейке.')
        channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            channel.bind((self.bind, 0))
            channel.setblocking(False)
            self.transfer = Transfer(channel, peer, packet, block_size, 0, b"", 0)
            LOG.info("Начата передача: %d байт, размер блока %d", len(self.image), block_size)
            if self.progress:
                self.progress(0, len(self.image))
            if options:
                self.transfer.negotiating = True
                body = b"".join(key.encode("ascii") + b"\0" + value.encode("ascii") + b"\0"
                                for key, value in options.items())
                self._send(struct.pack("!H", OACK) + body)
            else:
                self.transfer.block = 1
                self._data()
        except OSError:
            if self.transfer:
                self._finish("Ошибка передачи")
            else:
                channel.close()

    def _acknowledgement(self) -> None:
        transfer = self.transfer
        packet, peer = transfer.sock.recvfrom(65535)
        if peer != transfer.peer:
            self._error(transfer.sock, peer, 5, "Unknown transfer ID")
            return
        if len(packet) >= 2 and struct.unpack("!H", packet[:2])[0] == ERROR:
            self._finish("Передача отменена")
            return
        if len(packet) != 4 or struct.unpack("!H", packet[:2])[0] != ACK:
            self._error(transfer.sock, peer, 4, "Expected acknowledgement")
            self._finish("Неверное подтверждение получения")
            return
        block = struct.unpack("!H", packet[2:])[0]
        if block != transfer.block:
            return  # Duplicate or future ACK must not advance or reset the timer.
        if transfer.negotiating:
            transfer.negotiating = False
            transfer.block = 1
            self._data()
            return
        transfer.offset += transfer.payload_size
        if transfer.payload_size < transfer.block_size:
            if self.progress:
                self.progress(transfer.offset, len(self.image))
            self._finish("Передача завершена")
            return
        if self.progress and transfer.offset < len(self.image):
            self.progress(transfer.offset, len(self.image))
        if transfer.offset - transfer.reported >= 65536:
            if not self.progress:
                LOG.info("Передано %d из %d байт (%d%%)", transfer.offset, len(self.image),
                         transfer.offset * 100 // len(self.image))
            transfer.reported = transfer.offset
        transfer.block = (transfer.block + 1) & 0xffff
        self._data()  # Exact block-size multiples finish with an empty DATA block.

    def _timeout(self) -> None:
        transfer = self.transfer
        if not transfer or time.monotonic() < transfer.deadline:
            return
        if transfer.retries >= self.max_retries:
            self._finish("Время ожидания передачи истекло")
            return
        transfer.retries += 1
        transfer.deadline = time.monotonic() + self.timeout
        try:
            transfer.sock.sendto(transfer.packet, transfer.peer)
        except OSError:
            self._finish("Ошибка передачи")

    def serve_forever(self) -> None:
        LOG.info("Сервер готов на UDP-порту %d; доступен только KN-1112_recovery.bin", self.address[1])
        try:
            while not self.stop_event.is_set():
                readers = [self.sock]
                wait = 0.2
                if self.transfer:
                    readers.append(self.transfer.sock)
                    wait = min(wait, max(0, self.transfer.deadline - time.monotonic()))
                ready, _, _ = select.select(readers, [], [], wait)
                for channel in ready:
                    try:
                        if channel is self.sock:
                            self._request()
                        elif self.transfer and channel is self.transfer.sock:
                            self._acknowledgement()
                    except BlockingIOError:
                        pass
                    except OSError:
                        self._finish("Ошибка передачи")
                self._timeout()
        finally:
            self._finish("Сервер остановлен")
            self.sock.close()


class RecoveryParser(argparse.ArgumentParser):
    def format_usage(self):
        return super().format_usage().replace('usage:', 'Использование:', 1)

    def format_help(self):
        return super().format_help().replace('usage:', 'Использование:', 1).replace('options:', 'Параметры:')

    def error(self, message):
        for original, translated in [('the following arguments are required:', 'не указаны обязательные параметры:'),
                                     ('unrecognized arguments:', 'неизвестные параметры:'),
                                     ('expected one argument', 'ожидается одно значение'),
                                     ('invalid int value:', 'ожидается целое число:'), ('argument ', 'параметр ')]:
            message = message.replace(original, translated)
        self.print_usage(sys.stderr)
        self.exit(2, 'Ошибка: ' + message + '\n')


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure') and not stream.isatty():
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = RecoveryParser(description="Сервер первичной прошивки Keenetic KN-1112", add_help=False)
    parser.add_argument('-h', '--help', action='help', help='Показать справку и выйти')
    parser.add_argument("--bind", required=True, help="IPv4-адрес проводного адаптера компьютера")
    parser.add_argument("--image", required=True, type=Path, help="Проверенный образ factory .bin")
    parser.add_argument("--port", type=int, default=69, help="Порт UDP; по умолчанию 69")
    args = parser.parse_args(argv)
    try:
        validate_bind(args.bind)
        if not 1 <= args.port <= 65535:
            raise ValueError("Порт должен быть от 1 до 65535")
        server = RecoveryServer(args.image, args.bind, args.port)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Остановлено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
