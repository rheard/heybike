#!/usr/bin/env python3
"""
Flash a HeyBike/YuLai YMODE OTA firmware file over BLE.

This recreates the app's YMODE flow for non-88 IMEIs:
1. Connect and fetch the live BLE token.
2. Send encrypted opcode 0x35 with no payload.
3. If the bootloader is not already sending YMODEM control bytes, wait about
   two seconds and send encrypted opcode 0x35 with payload 0x01.
4. Send the firmware file using the app's 128-byte YMODEM variant. By default,
   send block 1 immediately after the header ACK because some HeyBike receivers
   never send the app-expected second CRC request.
5. Treat an ACK after end-of-transfer as completion; these receivers do not
   consistently request the standard final empty YMODEM header.

Only use this with official firmware for your own bike. Interrupting OTA can
brick the controller or IoT module.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import math
import pathlib
import sys
from dataclasses import dataclass
from typing import Optional


SERVICE_UUID = "86531001-43e6-47b7-9cb0-5fc21d4ae340"
WRITE_UUID = "86531002-43e6-47b7-9cb0-5fc21d4ae340"
NOTIFY_UUID = "86531003-43e6-47b7-9cb0-5fc21d4ae340"
NATIVE_KEY_SECRET = "a70948d8a93b9dab:0102930405060708"
NATIVE_COMMAND_TRANSFORMATION = "AES/ECB/NoPadding"
NATIVE_KEY_TRANSFORMATION = "AES/CBC/PKCS5Padding"
TOKEN_PREFIX = bytes([0x16, 0x5A, 0x01])
TOKEN_FETCH_CLEAR = TOKEN_PREFIX + bytes(13)
OTA_START_OPCODE = 0x35

SOH = 0x01
EOT = 0x04
ACK = 0x06
NAK = 0x15
CRC_REQUEST = 0x43
CPMEOF = 0x1A
YMODEM_BLOCK_SIZE = 128
YMODEM_CONTROL_BYTES = {ACK, NAK, CRC_REQUEST, EOT}


class FirmwareUpdateError(RuntimeError):
    pass


@dataclass
class CryptoConfig:
    command_transformation: str = NATIVE_COMMAND_TRANSFORMATION
    key_transformation: str = NATIVE_KEY_TRANSFORMATION


def hx(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def crc16_ccitt(data: bytes) -> int:
    crc = 0
    for byte in data:
        for bit in range(8):
            data_bit = ((byte >> (7 - bit)) & 1) == 1
            crc_bit = ((crc >> 15) & 1) == 1
            crc = (crc << 1) & 0xFFFF
            if data_bit ^ crc_bit:
                crc ^= 0x1021
    return crc & 0xFFFF


def build_command_frame(opcode: int, payload: bytes, token: bytes) -> bytes:
    if len(payload) > 8:
        raise ValueError("payload must fit in bytes 4..11")
    if len(token) != 4:
        raise ValueError("token must be exactly 4 bytes")
    frame = bytearray(16)
    frame[0] = 0x61
    frame[1] = 0x62
    frame[2] = opcode & 0xFF
    frame[3] = len(payload)
    frame[4 : 4 + len(payload)] = payload
    frame[12:16] = token
    return bytes(frame)


def complete_future_once(future: asyncio.Future, value: bytes) -> None:
    if not future.done():
        future.set_result(value)


class YModem128:
    def __init__(
        self,
        firmware_path: pathlib.Path,
        transfer_name: Optional[str] = None,
        *,
        send_first_block_after_header_ack: bool = True,
    ):
        self.path = firmware_path
        self.data = firmware_path.read_bytes()
        if not self.data:
            raise FirmwareUpdateError("firmware file is empty")
        self.transfer_name = transfer_name or firmware_path.name
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
        self._last_data_packet: Optional[bytes] = None
        self._nak_retries = 0

    def _packet(self, seq: int, payload: bytes) -> bytes:
        if len(payload) != YMODEM_BLOCK_SIZE:
            raise ValueError("YMODEM payload must be exactly 128 bytes")
        seq_byte = seq & 0xFF
        crc = crc16_ccitt(payload)
        return bytes([SOH, seq_byte, (0xFF - seq_byte) & 0xFF]) + payload + bytes(
            [(crc >> 8) & 0xFF, crc & 0xFF]
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
            chunk = chunk + bytes([CPMEOF]) * (YMODEM_BLOCK_SIZE - len(chunk))
        return self._packet(block_number, chunk)

    def _empty_final_packet(self) -> bytes:
        return self._packet(0, bytes(YMODEM_BLOCK_SIZE))

    def handle_byte(self, byte: int) -> tuple[bool, Optional[bytes], str]:
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

    def _send_next_block(self, prefix: Optional[str] = None) -> tuple[bool, Optional[bytes], str]:
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


async def get_runtime_helpers():
    try:
        from bleak import BleakClient
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "Missing dependency 'bleak'. Install with: python -m pip install bleak pycryptodome"
        ) from exc

    from heybike_power import (
        aes_crypt,
        api_login,
        api_user_bikes,
        choose_account_bike,
        decrypt_notification,
        decrypt_server_ble_key,
        resolve_device,
    )

    return {
        "BleakClient": BleakClient,
        "aes_crypt": aes_crypt,
        "api_login": api_login,
        "api_user_bikes": api_user_bikes,
        "choose_account_bike": choose_account_bike,
        "decrypt_notification": decrypt_notification,
        "decrypt_server_ble_key": decrypt_server_ble_key,
        "resolve_device": resolve_device,
    }


def require_confirmation(args: argparse.Namespace, firmware_path: pathlib.Path, engine: YModem128) -> None:
    if args.yes:
        return
    if args.non_interactive:
        raise FirmwareUpdateError("non-interactive OTA requires --yes")
    print()
    print("Firmware OTA can brick the bike if interrupted or if the file is wrong.")
    print(f"Firmware file: {firmware_path}")
    print(f"Transfer name: {engine.transfer_name}")
    print(f"Size: {len(engine.data)} bytes, YMODEM blocks: {engine.total_blocks}")
    print("Use only official firmware for your own bike, with the bike stationary and battery charged.")
    answer = input("Type FLASH to start OTA: ").strip()
    if answer != "FLASH":
        raise FirmwareUpdateError("confirmation not provided; aborting")


async def maybe_request_mtu(client, mtu: int) -> None:
    request_mtu = getattr(client, "request_mtu", None)
    if callable(request_mtu):
        try:
            result = request_mtu(mtu)
            if asyncio.iscoroutine(result):
                await result
            print(f"Requested BLE MTU {mtu}.")
        except Exception as exc:
            print(f"Warning: MTU request failed: {exc}")
    else:
        print("Warning: this Bleak backend does not expose request_mtu(); continuing anyway.")


async def run_update(args: argparse.Namespace) -> int:
    firmware_path = pathlib.Path(args.firmware)
    if not firmware_path.exists():
        raise FirmwareUpdateError(f"firmware file does not exist: {firmware_path}")
    if not firmware_path.is_file():
        raise FirmwareUpdateError(f"firmware path is not a file: {firmware_path}")

    engine = YModem128(
        firmware_path,
        args.transfer_name,
        send_first_block_after_header_ack=not args.wait_for_block_crc_after_header_ack,
    )

    helpers = await get_runtime_helpers()
    BleakClient = helpers["BleakClient"]
    aes_crypt = helpers["aes_crypt"]
    api_login = helpers["api_login"]
    api_user_bikes = helpers["api_user_bikes"]
    choose_account_bike = helpers["choose_account_bike"]
    decrypt_notification = helpers["decrypt_notification"]
    decrypt_server_ble_key = helpers["decrypt_server_ble_key"]
    resolve_device = helpers["resolve_device"]

    crypto = CryptoConfig(
        command_transformation=args.command_transformation,
        key_transformation=args.key_transformation,
    )

    account_bike = None
    encrypted_key = args.encrypted_key
    imei = args.imei or ""

    if args.email or args.api_token:
        api_token = args.api_token
        if not api_token:
            password = args.password
            if password is None:
                password = getpass.getpass(f"HeyBike password for {args.email}: ")
            print("Logging in to HeyBike API...")
            api_token = api_login(args.email, password)
        print("Fetching account bikes...")
        account_bike = choose_account_bike(
            api_user_bikes(api_token),
            requested_mac=args.bike_mac,
            requested_name=args.bike_name,
            requested_index=args.bike_index,
            non_interactive=args.non_interactive,
        )
        print(f"Selected bike: {account_bike.name}  {account_bike.mac}")
        encrypted_key = account_bike.encrypted_ble_key
        if not imei:
            imei = account_bike.imei

    if imei.startswith("88") and not args.force_ymode:
        raise FirmwareUpdateError(
            "IMEI prefix 88 uses LIANZHAO OTA in the app; this script supports YMODE only. "
            "Pass --force-ymode only if you know this bike actually uses YMODE."
        )

    if args.key:
        ble_key = args.key
    elif encrypted_key:
        ble_key = decrypt_server_ble_key(
            encrypted_key,
            args.native_key_secret,
            crypto.key_transformation,
        )
    else:
        raise FirmwareUpdateError("provide --email, --api-token, --key, or --encrypted-key")

    require_confirmation(args, firmware_path, engine)

    address = await resolve_device(
        args.address,
        args.name,
        args.scan_seconds,
        account_bike=account_bike,
        non_interactive=args.non_interactive,
        service_uuid=args.service_uuid,
    )

    loop = asyncio.get_running_loop()
    token_future: asyncio.Future[bytes] = loop.create_future()
    ota_ready_future: asyncio.Future[bytes] = loop.create_future()
    start_ack_queue: asyncio.Queue[bytes] = asyncio.Queue()
    raw_ota_queue: asyncio.Queue[bytes] = asyncio.Queue()
    ota_enabled = False

    def on_notify(_: int, data: bytearray) -> None:
        nonlocal ota_enabled
        raw = bytes(data)
        if args.verbose:
            print(f"notify raw: {hx(raw)}")
        try:
            clear = decrypt_notification(raw, ble_key, crypto)
        except Exception as exc:
            if args.verbose:
                print(f"notify decrypt failed: {exc}")
            clear = None
        if clear is not None:
            if args.verbose:
                print(f"notify clear: {hx(clear)}")
            if clear[:3] == TOKEN_PREFIX and not token_future.done():
                loop.call_soon_threadsafe(complete_future_once, token_future, clear[4:8])
                return
            if len(clear) >= 4 and clear[0] == 0x61 and clear[1] == 0x62 and clear[2] == OTA_START_OPCODE:
                loop.call_soon_threadsafe(start_ack_queue.put_nowait, clear)
                return
            if len(raw) == 18 and raw[0] == 0x7B and raw[-1] == 0x7D:
                return
        if ota_enabled:
            if any(byte in YMODEM_CONTROL_BYTES for byte in raw):
                loop.call_soon_threadsafe(complete_future_once, ota_ready_future, raw)
            loop.call_soon_threadsafe(raw_ota_queue.put_nowait, raw)

    async def write_encrypted_start_command(client, token: bytes, payload: bytes) -> None:
        frame = build_command_frame(OTA_START_OPCODE, payload, token)
        encrypted = aes_crypt(frame, ble_key, crypto.command_transformation, decrypt=False)
        if args.verbose:
            print(f"write OTA start clear:     {hx(frame)}")
            print(f"write OTA start encrypted: {hx(encrypted)}")
        await client.write_gatt_char(
            args.write_uuid,
            encrypted,
            response=not args.no_response,
        )

    async def wait_for_start_confirmation(label: str) -> tuple[str, bytes]:
        ack_task = asyncio.create_task(start_ack_queue.get())
        done, _ = await asyncio.wait(
            {ack_task, ota_ready_future},
            timeout=args.command_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            ack_task.cancel()
            raise FirmwareUpdateError(
                f"timed out waiting for {label}: no encrypted 0x35 ACK or raw YMODEM control byte"
            )
        if ack_task in done:
            return "ack", ack_task.result()
        ack_task.cancel()
        return "raw", ota_ready_future.result()

    async with BleakClient(address, timeout=args.connect_timeout) as client:
        if not client.is_connected:
            raise FirmwareUpdateError("BLE connection failed")
        print(f"Connected to {address}")

        await maybe_request_mtu(client, args.mtu)
        await client.start_notify(args.notify_uuid, on_notify)
        await asyncio.sleep(0.2)

        token_fetch_encrypted = aes_crypt(
            TOKEN_FETCH_CLEAR,
            ble_key,
            crypto.command_transformation,
            decrypt=False,
        )
        if args.verbose:
            print(f"write token encrypted: {hx(token_fetch_encrypted)}")
        await client.write_gatt_char(
            args.write_uuid,
            token_fetch_encrypted,
            response=not args.no_response,
        )
        try:
            token = await asyncio.wait_for(token_future, timeout=args.command_timeout)
        except asyncio.TimeoutError as exc:
            raise FirmwareUpdateError("timed out waiting for BLE token response") from exc
        print(f"Token: {hx(token)}")

        print("Sending OTA start command 0x35...")
        ota_enabled = True
        await write_encrypted_start_command(client, token, b"")
        kind0, response0 = await wait_for_start_confirmation("first OTA start command")
        if kind0 == "ack":
            print(f"OTA start response: {hx(response0)}")
        else:
            print(f"OTA start raw readiness: {hx(response0)}")

        if ota_ready_future.done():
            print("Raw YMODEM receiver is already ready; skipping second OTA start command.")
        else:
            print(f"Waiting {args.second_start_delay:.1f}s before second OTA start command...")
            await asyncio.sleep(args.second_start_delay)
            if ota_ready_future.done():
                print("Raw YMODEM receiver became ready; skipping second OTA start command.")
            else:
                await write_encrypted_start_command(client, token, b"\x01")
                kind1, response1 = await wait_for_start_confirmation("second OTA start command")
                if kind1 == "ack":
                    print(f"OTA second start response: {hx(response1)}")
                else:
                    print(f"OTA second start raw readiness: {hx(response1)}")

        print("Starting YMODEM transfer. Do not power off the bike or stop this process.")
        last_progress_print = -1
        while not engine.done:
            try:
                raw = await asyncio.wait_for(raw_ota_queue.get(), timeout=args.ota_response_timeout)
            except asyncio.TimeoutError as exc:
                raise FirmwareUpdateError(
                    f"timed out waiting for YMODEM receiver byte after {args.ota_response_timeout:.1f}s"
                ) from exc
            for byte in raw:
                done, packet, event = engine.handle_byte(byte)
                if args.verbose or event.startswith(("send", "all blocks", "final")):
                    print(f"YMODEM recv 0x{byte:02X}: {event}")
                if packet is not None:
                    if args.verbose:
                        print(f"write YMODEM ({len(packet)} bytes): {hx(packet[:32])}{' ...' if len(packet) > 32 else ''}")
                    await client.write_gatt_char(
                        args.write_uuid,
                        packet,
                        response=not args.no_response,
                    )
                    if engine.progress != last_progress_print and (
                        engine.progress == 100
                        or engine.progress - last_progress_print >= args.progress_step
                    ):
                        last_progress_print = engine.progress
                        print(f"Progress: {engine.progress}%")
                if done:
                    break

        print("YMODEM transfer complete. Waiting briefly for the bike to settle...")
        await asyncio.sleep(args.settle_seconds)
        try:
            await client.stop_notify(args.notify_uuid)
        except Exception:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flash HeyBike YMODE OTA firmware over BLE.")
    parser.add_argument("firmware", help="Firmware file to send, e.g. upgradeFile_10.vmfw.")
    parser.add_argument("--transfer-name", help="Filename to put in the YMODEM header. Defaults to firmware basename.")
    parser.add_argument("--yes", action="store_true", help="Skip interactive FLASH confirmation.")
    parser.add_argument("--force-ymode", action="store_true", help="Allow YMODE even if IMEI starts with 88.")
    parser.add_argument("--email", help="HeyBike account email. Enables automatic bike/key lookup.")
    parser.add_argument("--password", help="HeyBike password. If omitted with --email, prompts securely.")
    parser.add_argument("--api-token", help="Existing HeyBike API token.")
    parser.add_argument("--bike-index", type=int)
    parser.add_argument("--bike-name")
    parser.add_argument("--bike-mac")
    parser.add_argument("--imei", help="Bike IMEI. Used only to reject unsupported LIANZHAO prefix 88.")
    parser.add_argument("--address", help="BLE address/MAC. Usually unnecessary with --email.")
    parser.add_argument("--name", help="Scan for a BLE device whose name contains this text.")
    parser.add_argument("--scan-seconds", type=float, default=8.0)
    parser.add_argument("--key", help="Transformed/plain BLE command key.")
    parser.add_argument("--encrypted-key", help="Server-provided bleKey from the app/API.")
    parser.add_argument("--native-key-secret", default=NATIVE_KEY_SECRET)
    parser.add_argument("--command-transformation", default=NATIVE_COMMAND_TRANSFORMATION)
    parser.add_argument("--key-transformation", default=NATIVE_KEY_TRANSFORMATION)
    parser.add_argument("--service-uuid", default=SERVICE_UUID)
    parser.add_argument("--write-uuid", default=WRITE_UUID)
    parser.add_argument("--notify-uuid", default=NOTIFY_UUID)
    parser.add_argument("--mtu", type=int, default=150)
    parser.add_argument("--command-timeout", type=float, default=10.0)
    parser.add_argument("--ota-response-timeout", type=float, default=60.0)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--second-start-delay", type=float, default=2.0)
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--progress-step", type=int, default=5)
    parser.add_argument("--no-response", action="store_true", help="Use BLE write-without-response.")
    parser.add_argument(
        "--wait-for-block-crc-after-header-ack",
        action="store_true",
        help="Match the app exactly by waiting for a second YMODEM 'C' after the header ACK.",
    )
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run_update(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
