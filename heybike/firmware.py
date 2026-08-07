from __future__ import annotations

import math

from dataclasses import dataclass, field
from typing import Any

SOH = 0x01
EOT = 0x04
ACK = 0x06
NAK = 0x15
CRC_REQUEST = 0x43
CPMEOF = 0x1A
YMODEM_BLOCK_SIZE = 128
YMODEM_CONTROL_BYTES = {ACK, NAK, CRC_REQUEST, EOT}


class FirmwareUpdateError(RuntimeError):
    """Raised when a firmware check, download, or OTA transfer fails."""


@dataclass(frozen=True)
class FirmwareUpdate:
    """A firmware update offered by the HeyBike API."""

    current_hardware_version: int
    current_iot_firmware_version: int
    hardware_version: int
    iot_firmware_version: int
    mode: str
    filename: str
    ota_url: str
    ftp_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def current_version(self) -> tuple[int, int]:
        """The current bike-side IOT version as `(hardware, firmware)`."""

        return self.current_hardware_version, self.current_iot_firmware_version

    @property
    def version(self) -> tuple[int, int]:
        """The offered IOT version as `(hardware, firmware)`."""

        return self.hardware_version, self.iot_firmware_version


def firmware_mode_and_name(imei: str, ota_info: dict[str, Any]) -> tuple[str, str]:
    """Return the OTA transport mode and app-side filename for an OTA response."""

    ota_version = ota_info.get("otaVersion", 0)
    if imei.startswith("88"):
        return "LIANZHAO", f"upgradeFile_{ota_version}.bin"
    if imei.startswith(("85", "86")):
        return "YMODE", "app.bin"
    return "YMODE", f"upgradeFile_{ota_version}.vmfw"


def crc16_ccitt(data: bytes) -> int:
    """Return the CRC-16/CCITT value used by YMODEM packets."""

    crc = 0
    for byte in data:
        for bit in range(8):
            data_bit = ((byte >> (7 - bit)) & 1) == 1
            crc_bit = ((crc >> 15) & 1) == 1
            crc = (crc << 1) & 0xFFFF
            if data_bit ^ crc_bit:
                crc ^= 0x1021
    return crc & 0xFFFF


class YModem128:
    """State machine for HeyBike's 128-byte YMODEM OTA variant."""

    def __init__(
        self,
        data: bytes,
        transfer_name: str,
        *,
        send_first_block_after_header_ack: bool = True,
    ):
        """Initialize a sender for one in-memory firmware image."""

        if not data:
            raise FirmwareUpdateError("firmware image is empty")
        self.data = bytes(data)
        self.transfer_name = transfer_name
        self.send_first_block_after_header_ack = send_first_block_after_header_ack
        self.total_blocks = math.ceil(len(self.data) / YMODEM_BLOCK_SIZE)
        self.next_block = 1
        self.progress = 0
        self.done = False
        self._initial = True
        self._header_sent = False
        self._waiting_for_block_crc = False
        self._no_more_blocks = False
        self._final_packet_requested = False
        self._final_packet_sent = False
        self._last_data_packet: bytes | None = None
        self._nak_retries = 0

    def _packet(self, sequence: int, payload: bytes) -> bytes:
        if len(payload) != YMODEM_BLOCK_SIZE:
            raise ValueError("YMODEM payload must be exactly 128 bytes")
        sequence_byte = sequence & 0xFF
        crc = crc16_ccitt(payload)
        return (
            bytes([SOH, sequence_byte, (0xFF - sequence_byte) & 0xFF])
            + payload
            + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
        )

    def _header_packet(self) -> bytes:
        payload = bytearray(YMODEM_BLOCK_SIZE)
        name = self.transfer_name.encode("ascii", errors="ignore")
        size = str(len(self.data)).encode("ascii")
        if len(name) + 1 + len(size) + 1 > YMODEM_BLOCK_SIZE:
            raise FirmwareUpdateError("YMODEM transfer filename is too long")
        payload[: len(name)] = name
        payload[len(name)] = 0
        size_start = len(name) + 1
        payload[size_start : size_start + len(size)] = size
        payload[size_start + len(size)] = 0
        return self._packet(0, bytes(payload))

    def _data_packet(self, block_number: int) -> bytes:
        start = (block_number - 1) * YMODEM_BLOCK_SIZE
        chunk = self.data[start : start + YMODEM_BLOCK_SIZE]
        if len(chunk) < YMODEM_BLOCK_SIZE:
            chunk += bytes([CPMEOF]) * (YMODEM_BLOCK_SIZE - len(chunk))
        return self._packet(block_number, chunk)

    def _empty_final_packet(self) -> bytes:
        return self._packet(0, bytes(YMODEM_BLOCK_SIZE))

    def handle_byte(self, byte: int) -> tuple[bool, bytes | None, str]:
        """Handle one receiver byte and return `(done, packet_to_send, event)`."""

        if self.done:
            return True, None, "already complete"

        if byte == CRC_REQUEST:
            self._nak_retries = 0
            if self._initial:
                self._initial = False
                self._header_sent = True
                return False, self._header_packet(), "send YMODEM header"
            if self._final_packet_requested:
                self._final_packet_requested = False
                self._final_packet_sent = True
                return False, self._empty_final_packet(), "send final empty packet"
            if self._waiting_for_block_crc:
                self._waiting_for_block_crc = False
                return self._send_next_block()
            return False, None, "CRC request ignored in current state"

        if byte == ACK:
            self._nak_retries = 0
            if self._final_packet_sent:
                self.done = True
                self.progress = 100
                return True, None, "final ACK; OTA transfer complete"
            if self._header_sent:
                self._header_sent = False
                if self.send_first_block_after_header_ack:
                    return self._send_next_block("header ACK; send first block")
                self._waiting_for_block_crc = True
                return False, None, "header ACK; waiting for block CRC request"
            if self._no_more_blocks:
                self.done = True
                self.progress = 100
                return True, None, "ACK after EOT; OTA transfer complete"
            return self._send_next_block()

        if byte == NAK:
            if self._no_more_blocks:
                self._final_packet_requested = True
                return False, bytes([EOT]), "NAK after EOT; send EOT again"
            if self._last_data_packet is not None and self._nak_retries < 3:
                self._nak_retries += 1
                return False, self._last_data_packet, f"NAK; resend block (retry {self._nak_retries})"
            raise FirmwareUpdateError("YMODEM NAK retry limit reached")

        return False, None, f"ignored receiver byte 0x{byte:02X}"

    def _send_next_block(self, prefix: str | None = None) -> tuple[bool, bytes | None, str]:
        if self.next_block > self.total_blocks:
            self._no_more_blocks = True
            return False, bytes([EOT]), "all blocks sent; send EOT"
        packet = self._data_packet(self.next_block)
        self._last_data_packet = packet
        self.progress = int((self.next_block * 100.0) / self.total_blocks)
        event = f"send block {self.next_block}/{self.total_blocks} ({self.progress}%)"
        if prefix:
            event = f"{prefix}; {event}"
        self.next_block += 1
        return False, packet, event
