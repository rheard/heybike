#!/usr/bin/env python3
"""
Read or set the HeyBike/YuLai max-speed value over BLE.

This uses the same account/key/token flow as heybike_power.py. The app's
max-speed command is opcode 0xE1:

  read:  payload length 0
  write: payload length 2, big-endian integer value

For bikes whose IMEI starts with "86", the app converts displayed speed to the
raw BLE value with round(display_value * 0.62137), and converts raw reads back
with round(raw_value / 0.62137). This script does the same by default when the
IMEI is known. Pass --raw to read/set raw BLE values directly.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import math
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
MAX_SPEED_OPCODE = 0xE1
MPH_FACTOR = 0.62137


class MaxSpeedError(RuntimeError):
    pass


@dataclass
class CryptoConfig:
    command_transformation: str = NATIVE_COMMAND_TRANSFORMATION
    key_transformation: str = NATIVE_KEY_TRANSFORMATION


def hx(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def u16be(data: bytes, offset: int) -> Optional[int]:
    if len(data) <= offset + 1:
        return None
    return ((data[offset] << 8) & 0xFF00) | data[offset + 1]


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def raw_to_app_value(raw_value: int, imei: str, raw_mode: bool) -> int:
    if raw_mode or not imei.startswith("86"):
        return raw_value
    return round_half_up(raw_value / MPH_FACTOR)


def app_value_to_raw(value: int, imei: str, raw_mode: bool) -> int:
    if raw_mode or not imei.startswith("86"):
        return value
    return round_half_up(value * MPH_FACTOR)


def value_payload(value: int) -> bytes:
    if value < 0 or value > 0xFFFF:
        raise MaxSpeedError("max-speed raw value must be between 0 and 65535")
    return bytes([(value >> 8) & 0xFF, value & 0xFF])


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


def parse_max_speed_response(clear: bytes) -> int:
    if (
        len(clear) < 6
        or clear[0] != 0x61
        or clear[1] != 0x62
        or clear[2] != MAX_SPEED_OPCODE
    ):
        raise MaxSpeedError(f"not a max-speed response: {hx(clear)}")
    value = u16be(clear, 4)
    if value is None:
        raise MaxSpeedError(f"max-speed response too short: {hx(clear)}")
    return value


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


async def run_max_speed(args: argparse.Namespace) -> int:
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

    if args.key:
        ble_key = args.key
    elif encrypted_key:
        ble_key = decrypt_server_ble_key(
            encrypted_key,
            args.native_key_secret,
            crypto.key_transformation,
        )
    else:
        raise MaxSpeedError("provide --email, --api-token, --key, or --encrypted-key")

    if imei and imei.startswith("86") and not args.raw:
        print("Using app-style conversion for IMEI prefix 86.")
    elif not imei and not args.raw:
        print("No IMEI known; using raw/app value without model conversion.")

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
    max_speed_queue: asyncio.Queue[bytes] = asyncio.Queue()

    def on_notify(_: int, data: bytearray) -> None:
        raw = bytes(data)
        if args.verbose:
            print(f"notify raw: {hx(raw)}")
        try:
            clear = decrypt_notification(raw, ble_key, crypto)
        except Exception as exc:
            if args.verbose:
                print(f"notify decrypt failed: {exc}")
            return
        if clear is None:
            return
        if args.verbose:
            print(f"notify clear: {hx(clear)}")
        if clear[:3] == TOKEN_PREFIX and not token_future.done():
            loop.call_soon_threadsafe(token_future.set_result, clear[4:8])
            return
        if len(clear) >= 4 and clear[0] == 0x61 and clear[1] == 0x62 and clear[2] == MAX_SPEED_OPCODE:
            loop.call_soon_threadsafe(max_speed_queue.put_nowait, clear)

    async def send_max_speed_command(client, token: bytes, payload: bytes) -> int:
        frame = build_command_frame(MAX_SPEED_OPCODE, payload, token)
        encrypted = aes_crypt(frame, ble_key, crypto.command_transformation, decrypt=False)
        if args.verbose:
            print(f"write clear:     {hx(frame)}")
            print(f"write encrypted: {hx(encrypted)}")
        await client.write_gatt_char(
            args.write_uuid,
            encrypted,
            response=not args.no_response,
        )
        clear = await asyncio.wait_for(max_speed_queue.get(), timeout=args.timeout)
        print(f"Max-speed response: {hx(clear)}")
        return parse_max_speed_response(clear)

    async with BleakClient(address, timeout=args.connect_timeout) as client:
        if not client.is_connected:
            raise MaxSpeedError("BLE connection failed")
        print(f"Connected to {address}")
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
        token = await asyncio.wait_for(token_future, timeout=args.timeout)
        print(f"Token: {hx(token)}")

        if args.action == "read":
            print("Reading max speed...")
            raw_value = await send_max_speed_command(client, token, b"")
            app_value = raw_to_app_value(raw_value, imei, args.raw)
            print(f"Max speed: {app_value} ({'raw' if args.raw else 'app value'}), raw BLE value: {raw_value}")
        else:
            if args.value is None:
                raise MaxSpeedError("set requires --value")
            raw_value = app_value_to_raw(args.value, imei, args.raw)
            payload = value_payload(raw_value)
            print(
                f"Setting max speed to {args.value} "
                f"({'raw' if args.raw else 'app value'}), raw BLE value {raw_value}..."
            )
            response_raw = await send_max_speed_command(client, token, payload)
            response_app = raw_to_app_value(response_raw, imei, args.raw)
            print(f"Max speed after write response: {response_app}, raw BLE value: {response_raw}")
            if args.read_after_write:
                await asyncio.sleep(args.command_delay)
                print("Reading max speed after write...")
                readback_raw = await send_max_speed_command(client, token, b"")
                readback_app = raw_to_app_value(readback_raw, imei, args.raw)
                print(f"Max speed readback: {readback_app}, raw BLE value: {readback_raw}")

        await client.stop_notify(args.notify_uuid)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read or set a HeyBike max-speed value over BLE.")
    parser.add_argument("action", choices=("read", "set"))
    parser.add_argument("--value", type=int, help="Value for action 'set'.")
    parser.add_argument("--raw", action="store_true", help="Read/set raw BLE values without app model conversion.")
    parser.add_argument("--imei", help="Bike IMEI; enables app conversion for IMEI prefix 86 when not using --raw.")
    parser.add_argument("--email", help="HeyBike account email. Enables automatic bike/key lookup.")
    parser.add_argument("--password", help="HeyBike password. If omitted with --email, prompts securely.")
    parser.add_argument("--api-token", help="Existing HeyBike API token.")
    parser.add_argument("--bike-index", type=int)
    parser.add_argument("--bike-name")
    parser.add_argument("--bike-mac")
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
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--command-delay", type=float, default=0.2)
    parser.add_argument("--no-response", action="store_true", help="Use BLE write-without-response.")
    parser.add_argument("--read-after-write", action="store_true", help="Send a read command after writes.")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run_max_speed(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
