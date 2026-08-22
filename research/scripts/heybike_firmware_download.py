#!/usr/bin/env python3
"""
Acquire HeyBike/YuLai firmware update files.

The app's OTA check does not download the current firmware from the bike. It:
1. Reads current IOT hard/version numbers over BLE.
2. Calls appHeyApi/getBikeIMEIUpload with deIMEI, hardVersion, oldVersion.
3. Downloads the returned otaUrl if present.

This script recreates that acquisition path. It does not flash firmware.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib import request

from bleak import BleakClient

from heybike_power import (
    NATIVE_KEY_SECRET,
    CryptoConfig,
    ProtocolError,
    TOKEN_FETCH_CLEAR,
    TOKEN_PREFIX,
    aes_crypt,
    api_login,
    api_post,
    api_user_bikes,
    choose_account_bike,
    decrypt_notification,
    decrypt_server_ble_key,
    hx,
    resolve_device,
)


BASE_INFO_OPCODE = 0x38


@dataclass
class BikeVersionInfo:
    hard_version: int
    old_version: int
    protocol_version: int
    power_level: int
    power_on: bool
    auto_lock: bool


def build_command_frame(opcode: int, payload: bytes = b"", token: bytes = b"\x00\x00\x00\x00") -> bytes:
    if len(payload) > 8:
        raise ValueError("normal command payload must fit before token bytes")
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


def parse_base_info(clear: bytes) -> BikeVersionInfo:
    if len(clear) < 11 or clear[0] != 0x61 or clear[1] != 0x62 or clear[2] != BASE_INFO_OPCODE:
        raise ProtocolError(f"not a base-info response: {hx(clear)}")
    return BikeVersionInfo(
        hard_version=clear[8] & 0xFF,
        old_version=clear[9] & 0xFF,
        protocol_version=clear[10] & 0xFF,
        power_level=clear[5] & 0xFF,
        power_on=clear[7] == 1,
        auto_lock=clear[6] == 1,
    )


async def read_bike_version(
    address: str,
    ble_key: str,
    crypto: CryptoConfig,
    *,
    write_uuid: str,
    notify_uuid: str,
    connect_timeout: float,
    timeout: float,
    no_response: bool,
    verbose: bool,
) -> BikeVersionInfo:
    loop = asyncio.get_running_loop()
    token_future: asyncio.Future[bytes] = loop.create_future()
    base_info_future: asyncio.Future[BikeVersionInfo] = loop.create_future()

    def on_notify(_: int, data: bytearray) -> None:
        raw = bytes(data)
        if verbose:
            print(f"notify raw: {hx(raw)}")
        try:
            clear = decrypt_notification(raw, ble_key, crypto)
        except Exception as exc:
            if verbose:
                print(f"notify decrypt failed: {exc}")
            return
        if clear is None:
            return
        if verbose:
            print(f"notify clear: {hx(clear)}")
        if clear[:3] == TOKEN_PREFIX and not token_future.done():
            loop.call_soon_threadsafe(token_future.set_result, clear[4:8])
            return
        if len(clear) >= 11 and clear[2] == BASE_INFO_OPCODE and not base_info_future.done():
            try:
                info = parse_base_info(clear)
            except Exception as exc:
                loop.call_soon_threadsafe(base_info_future.set_exception, exc)
            else:
                loop.call_soon_threadsafe(base_info_future.set_result, info)

    async with BleakClient(address, timeout=connect_timeout) as client:
        if not client.is_connected:
            raise ProtocolError("BLE connection failed")
        print(f"Connected to {address}")
        await client.start_notify(notify_uuid, on_notify)
        await asyncio.sleep(0.2)

        token_fetch_encrypted = aes_crypt(
            TOKEN_FETCH_CLEAR,
            ble_key,
            crypto.command_transformation,
            decrypt=False,
        )
        if verbose:
            print(f"write token encrypted: {hx(token_fetch_encrypted)}")
        await client.write_gatt_char(write_uuid, token_fetch_encrypted, response=not no_response)
        token = await asyncio.wait_for(token_future, timeout=timeout)
        print(f"Token: {hx(token)}")

        base_clear = build_command_frame(BASE_INFO_OPCODE, token=token)
        base_encrypted = aes_crypt(
            base_clear,
            ble_key,
            crypto.command_transformation,
            decrypt=False,
        )
        if verbose:
            print(f"write base-info clear:     {hx(base_clear)}")
            print(f"write base-info encrypted: {hx(base_encrypted)}")
        await client.write_gatt_char(write_uuid, base_encrypted, response=not no_response)
        info = await asyncio.wait_for(base_info_future, timeout=timeout)
        await client.stop_notify(notify_uuid)
        return info


def ota_check(token: str, imei: str, hard_version: int, old_version: int) -> dict:
    return api_post(
        "appHeyApi/getBikeIMEIUpload",
        {
            "token": token,
            "deIMEI": imei,
            "hardVersion": str(hard_version),
            "oldVersion": str(old_version),
        },
    )


def firmware_mode_and_name(imei: str, ota_info: dict) -> tuple[str, str]:
    ota_version = ota_info.get("otaVersion", 0)
    if imei.startswith("88"):
        return "LIANZHAO", f"upgradeFile_{ota_version}.bin"
    if imei.startswith("86") or imei.startswith("85"):
        return "YMODE", "app.bin"
    return "YMODE", f"upgradeFile_{ota_version}.vmfw"


def safe_filename(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)


def download_file(url: str, out_path: Path) -> None:
    req = request.Request(url, headers={"User-Agent": "okhttp/4.11.0"})
    with request.urlopen(req, timeout=120) as resp, out_path.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 128)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if total:
                pct = int(done * 100 / total)
                print(f"\rDownloading: {pct:3d}% ({done}/{total} bytes)", end="")
        if total:
            print()


def print_ota_summary(data: dict) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


async def main_async(args: argparse.Namespace) -> int:
    if not args.email and not args.api_token:
        raise ProtocolError("provide --email or --api-token")

    token = args.api_token
    if not token:
        import getpass

        password = args.password
        if password is None:
            password = getpass.getpass(f"HeyBike password for {args.email}: ")
        print("Logging in to HeyBike API...")
        token = api_login(args.email, password)

    print("Fetching account bikes...")
    bikes = api_user_bikes(token)
    bike = choose_account_bike(
        bikes,
        requested_mac=args.bike_mac,
        requested_name=args.bike_name,
        requested_index=args.bike_index,
        non_interactive=args.non_interactive,
    )
    if not bike.imei:
        raise ProtocolError("selected account bike has no deIMEI; cannot query OTA endpoint")
    print(f"Selected bike: {bike.name}  {bike.mac}  IMEI {bike.imei}")

    crypto = CryptoConfig(
        command_transformation=args.command_transformation,
        key_transformation=args.key_transformation,
    )

    if args.hard_version is not None and args.old_version is not None:
        version = BikeVersionInfo(
            hard_version=args.hard_version,
            old_version=args.old_version,
            protocol_version=-1,
            power_level=-1,
            power_on=False,
            auto_lock=False,
        )
        print(
            f"Using supplied current version: "
            f"{version.hard_version}.{version.old_version}"
        )
    else:
        ble_key = decrypt_server_ble_key(
            bike.encrypted_ble_key,
            args.native_key_secret,
            crypto.key_transformation,
        )
        address = await resolve_device(
            args.address,
            args.name,
            args.scan_seconds,
            account_bike=bike,
            non_interactive=args.non_interactive,
            service_uuid=args.service_uuid,
        )
        print("Reading current bike firmware version over BLE...")
        version = await read_bike_version(
            address,
            ble_key,
            crypto,
            write_uuid=args.write_uuid,
            notify_uuid=args.notify_uuid,
            connect_timeout=args.connect_timeout,
            timeout=args.timeout,
            no_response=args.no_response,
            verbose=args.verbose,
        )
        print(
            f"Current IOT version: {version.hard_version}.{version.old_version} "
            f"(protocol {version.protocol_version}, power {version.power_level}%)"
        )

    print("Checking OTA metadata...")
    ota = ota_check(token, bike.imei, version.hard_version, version.old_version)
    if args.print_json:
        print_ota_summary(ota)

    iot_info = ota.get("iotInfo") or {}
    if not isinstance(iot_info, dict):
        iot_info = {}
    ota_url = str(iot_info.get("otaUrl") or "")
    ftp_url = str(iot_info.get("ftpUrl") or "")
    if ftp_url:
        print(f"FTP/server-side OTA URL present: {ftp_url}")
    if not ota_url:
        print("No otaUrl returned. The server is not offering a downloadable app-side firmware update for this version.")
        return 0

    mode, app_filename = firmware_mode_and_name(bike.imei, iot_info)
    hard = iot_info.get("otaHardVersion", "unknown")
    ver = iot_info.get("otaVersion", "unknown")
    print(f"Available firmware: mode={mode} version={hard}.{ver}")
    print(f"otaUrl: {ota_url}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = args.output_name or app_filename
    out_path = out_dir / safe_filename(filename)
    meta_path = out_path.with_suffix(out_path.suffix + ".json")

    if args.metadata_only:
        print(f"Metadata only; not downloading. Suggested filename: {out_path}")
        meta_path.write_text(json.dumps(ota, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Wrote metadata: {meta_path}")
        return 0

    print(f"Downloading firmware to {out_path}...")
    download_file(ota_url, out_path)
    if out_path.stat().st_size <= 0:
        raise ProtocolError("downloaded firmware file is empty")
    meta_path.write_text(json.dumps(ota, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote firmware: {out_path} ({out_path.stat().st_size} bytes)")
    print(f"Wrote metadata: {meta_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download HeyBike OTA firmware metadata/files.")
    parser.add_argument("--email", help="HeyBike account email.")
    parser.add_argument("--password", help="HeyBike account password. Omit to prompt.")
    parser.add_argument("--api-token", help="Existing HeyBike API token.")
    parser.add_argument("--bike-index", type=int)
    parser.add_argument("--bike-name")
    parser.add_argument("--bike-mac")
    parser.add_argument("--address", help="Optional BLE address override.")
    parser.add_argument("--name", help="Optional BLE name filter.")
    parser.add_argument("--hard-version", type=int, help="Skip BLE and supply hardVersion.")
    parser.add_argument("--old-version", type=int, help="Skip BLE and supply oldVersion.")
    parser.add_argument("--output-dir", default="firmware")
    parser.add_argument("--output-name")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--print-json", action="store_true")
    parser.add_argument("--scan-seconds", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--no-response", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--service-uuid", default="86531001-43e6-47b7-9cb0-5fc21d4ae340")
    parser.add_argument("--write-uuid", default="86531002-43e6-47b7-9cb0-5fc21d4ae340")
    parser.add_argument("--notify-uuid", default="86531003-43e6-47b7-9cb0-5fc21d4ae340")
    parser.add_argument("--native-key-secret", default=NATIVE_KEY_SECRET)
    parser.add_argument("--command-transformation", default="AES/ECB/NoPadding")
    parser.add_argument("--key-transformation", default="AES/CBC/PKCS5Padding")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if (args.hard_version is None) ^ (args.old_version is None):
        parser.error("--hard-version and --old-version must be supplied together")
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
