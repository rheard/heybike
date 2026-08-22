#!/usr/bin/env python3
"""
Minimal HeyBike/YuLai BLE power control client.

This recreates the visible app flow for turning a bike on or off:
1. Connect to the normal HeyBike BLE service.
2. Subscribe to notifications.
3. Send the encrypted token-fetch command.
4. Decrypt the token response and copy bytes 4..7 as the live token.
5. Build opcode 0x31 with payload 0x01/on or 0x00/off.
6. Insert the live token at bytes 12..15, encrypt, and write it.

Only use this with a bike you own/control, and test with the bike stationary.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import getpass
import json
import platform
import sys
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional
from urllib import parse, request

try:
    from bleak import BleakClient, BleakScanner
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Missing dependency 'bleak'. Install with: python -m pip install bleak pycryptodome"
    ) from exc

try:
    from Crypto.Cipher import AES  # type: ignore
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "Missing dependency 'pycryptodome'. Install with: python -m pip install pycryptodome"
    ) from exc


SERVICE_UUID = "86531001-43e6-47b7-9cb0-5fc21d4ae340"
WRITE_UUID = "86531002-43e6-47b7-9cb0-5fc21d4ae340"
NOTIFY_UUID = "86531003-43e6-47b7-9cb0-5fc21d4ae340"
API_BASE_URL = "https://heyapi.heybike.com/"
APP_VERSION = "v4.6.0"
PHONE_TYPE = f"{platform.system()}/python:{platform.machine() or 'unknown'}"
PHONE_SYSTEMS = f"OS Version:{platform.platform()}"
LANGUAGE = "en"
COUNTRY_CODE = "US"
NATIVE_KEY_SECRET = "a70948d8a93b9dab:0102930405060708"
NATIVE_COMMAND_TRANSFORMATION = "AES/ECB/NoPadding"
NATIVE_KEY_TRANSFORMATION = "AES/CBC/PKCS5Padding"

TOKEN_PREFIX = bytes([0x16, 0x5A, 0x01])
TOKEN_FETCH_CLEAR = TOKEN_PREFIX + bytes(13)
POWER_OPCODE = 0x31


class ProtocolError(RuntimeError):
    pass


@dataclass
class CryptoConfig:
    command_transformation: str = NATIVE_COMMAND_TRANSFORMATION
    key_transformation: str = NATIVE_KEY_TRANSFORMATION


@dataclass
class AccountBike:
    name: str
    mac: str
    encrypted_ble_key: str
    imei: str = ""
    raw: Optional[dict[str, Any]] = None


@dataclass
class NearbyHeybike:
    mac: str
    name: str = ""
    rssi: Optional[int] = None
    service_uuids: tuple[str, ...] = ()
    manufacturer_data: dict[int, bytes] = field(default_factory=dict)
    service_data: dict[str, bytes] = field(default_factory=dict)
    tx_power: Optional[int] = None


def hx(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def parse_hex_bytes(value: str, expected_len: Optional[int] = None) -> bytes:
    compact = value.replace(":", "").replace(" ", "").replace("-", "")
    try:
        out = binascii.unhexlify(compact)
    except binascii.Error as exc:
        raise argparse.ArgumentTypeError(f"invalid hex bytes: {value!r}") from exc
    if expected_len is not None and len(out) != expected_len:
        raise argparse.ArgumentTypeError(
            f"expected {expected_len} bytes, got {len(out)} bytes"
        )
    return out


def compact_mac(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch in "0123456789ABCDEF")


def normalize_mac(value: str) -> str:
    compact = compact_mac(value)
    if len(compact) == 12:
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))
    return value.upper()


def is_heybike_name(name: Optional[str]) -> bool:
    return bool(name) and name.lower().startswith("heybike")


def device_rssi(device: Any) -> int:
    rssi = getattr(device, "rssi", None)
    return rssi if isinstance(rssi, int) else -999


def device_service_uuids(device: Any) -> list[str]:
    metadata = getattr(device, "metadata", None) or {}
    uuids = metadata.get("uuids") or []
    return [str(uuid).lower() for uuid in uuids]


def advertisement_service_uuids(advertisement_data: Any) -> list[str]:
    uuids = getattr(advertisement_data, "service_uuids", None) or []
    return [str(uuid).lower() for uuid in uuids]


def advertisement_name(device: Any, advertisement_data: Any) -> str:
    return (
        getattr(advertisement_data, "local_name", None)
        or getattr(device, "name", None)
        or ""
    )


def advertisement_rssi(device: Any, advertisement_data: Any) -> Optional[int]:
    for source in (advertisement_data, device):
        rssi = getattr(source, "rssi", None)
        if isinstance(rssi, int):
            return rssi
    return None


def advertisement_bytes_map(value: Any) -> dict[Any, bytes]:
    if not isinstance(value, dict):
        return {}
    out: dict[Any, bytes] = {}
    for key, data in value.items():
        if isinstance(data, bytes):
            out[key] = data
        elif isinstance(data, bytearray):
            out[key] = bytes(data)
    return out


async def iter_nearby_heybikes(
    scan_seconds: float = 20.0,
    *,
    name_prefix: str = "Heybike",
    service_uuid: Optional[str] = SERVICE_UUID,
) -> AsyncIterator[NearbyHeybike]:
    """Yield unique nearby HeyBike BLE advertisement records as they arrive.

    This deliberately performs discovery only. It does not connect, authenticate,
    fetch BLE keys, or send bike commands.
    """

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[NearbyHeybike] = asyncio.Queue()
    seen: set[str] = set()
    wanted_prefix = name_prefix.lower()
    wanted_service = service_uuid.lower() if service_uuid else None

    def enqueue_once(record: NearbyHeybike):
        key = compact_mac(record.mac) or record.mac.upper()
        if key in seen:
            return
        seen.add(key)
        queue.put_nowait(record)

    def on_advertisement(device: Any, advertisement_data: Any):
        name = advertisement_name(device, advertisement_data)
        service_uuids = (
            advertisement_service_uuids(advertisement_data)
            or device_service_uuids(device)
        )
        has_matching_name = bool(name) and name.lower().startswith(wanted_prefix)
        has_matching_service = bool(wanted_service and wanted_service in service_uuids)
        if not has_matching_name and not has_matching_service:
            return

        address = getattr(device, "address", "")
        if address:
            record = NearbyHeybike(
                mac=normalize_mac(address),
                name=name,
                rssi=advertisement_rssi(device, advertisement_data),
                service_uuids=tuple(service_uuids),
                manufacturer_data=advertisement_bytes_map(
                    getattr(advertisement_data, "manufacturer_data", None)
                ),
                service_data=advertisement_bytes_map(
                    getattr(advertisement_data, "service_data", None)
                ),
                tx_power=getattr(advertisement_data, "tx_power", None),
            )
            loop.call_soon_threadsafe(enqueue_once, record)

    deadline = loop.time() + scan_seconds
    async with BleakScanner(detection_callback=on_advertisement):
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                yield await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break


async def iter_nearby_heybike_macs(
    scan_seconds: float = 20.0,
    *,
    name_prefix: str = "Heybike",
    service_uuid: Optional[str] = SERVICE_UUID,
) -> AsyncIterator[str]:
    """Yield unique nearby HeyBike BLE MACs/addresses as advertisements arrive."""

    async for bike in iter_nearby_heybikes(
        scan_seconds=scan_seconds,
        name_prefix=name_prefix,
        service_uuid=service_uuid,
    ):
        yield bike.mac


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data or len(data) % block_size:
        raise ProtocolError("invalid padded data length")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        raise ProtocolError("invalid padding")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ProtocolError("invalid padding bytes")
    return data[:-pad_len]


def parse_transformation(transformation: str) -> tuple[str, str, str]:
    parts = transformation.split("/")
    if len(parts) == 1:
        return parts[0].upper(), "ECB", "PKCS5Padding"
    if len(parts) != 3:
        raise ValueError(f"unsupported cipher transformation: {transformation}")
    algorithm, mode, padding = parts
    return algorithm.upper(), mode.upper(), padding


def aes_crypt(
    data: bytes,
    key_text: str,
    transformation: str,
    *,
    decrypt: bool,
    iv_text: Optional[str] = None,
) -> bytes:
    algorithm, mode, padding = parse_transformation(transformation)
    if algorithm != "AES":
        raise ValueError(f"unsupported algorithm {algorithm!r}; expected AES")

    key = key_text.encode("utf-8")
    if len(key) not in (16, 24, 32):
        raise ValueError(
            f"AES key must be 16, 24, or 32 UTF-8 bytes; got {len(key)}"
        )

    if mode == "ECB":
        cipher = AES.new(key, AES.MODE_ECB)
    elif mode == "CBC":
        if iv_text is None:
            raise ValueError("CBC mode requires an IV")
        iv = iv_text.encode("utf-8")
        if len(iv) != 16:
            raise ValueError(f"AES CBC IV must be 16 UTF-8 bytes; got {len(iv)}")
        cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    else:
        raise ValueError(f"unsupported AES mode {mode!r}")

    padding_lower = padding.lower()
    use_padding = padding_lower in ("pkcs5padding", "pkcs7padding")
    no_padding = padding_lower == "nopadding"
    if not use_padding and not no_padding:
        raise ValueError(f"unsupported padding {padding!r}")

    if decrypt:
        result = cipher.decrypt(data)
        return pkcs7_unpad(result) if use_padding else result

    clear = data
    if use_padding:
        clear = pkcs7_pad(clear)
    elif len(clear) % 16:
        raise ValueError("NoPadding encryption requires a multiple of 16 bytes")
    return cipher.encrypt(clear)


def decrypt_server_ble_key(
    encrypted_ble_key: str,
    native_key_secret: str,
    transformation: str,
) -> str:
    try:
        key, iv = native_key_secret.split(":", 1)
    except ValueError as exc:
        raise ValueError(
            "native key secret must be in the form KEY:IV, matching NativeLib.getKeySecret()"
        ) from exc

    encrypted = base64.b64decode(encrypted_ble_key)
    clear = aes_crypt(encrypted, key, transformation, decrypt=True, iv_text=iv)
    return clear.decode("utf-8").strip()


def api_headers() -> dict[str, str]:
    return {
        "systemtype": "android",
        "phoneInfo": APP_VERSION,
        "phoneType": PHONE_TYPE,
        "phoneSystems": PHONE_SYSTEMS,
        "source": "1",
        "language": LANGUAGE,
        "countryCode": COUNTRY_CODE,
        "User-Agent": "okhttp/4.11.0",
    }


def api_post(endpoint: str, fields: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    body = parse.urlencode(fields).encode("utf-8")
    req = request.Request(
        parse.urljoin(API_BASE_URL, endpoint),
        data=body,
        headers={
            **api_headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"API returned non-JSON from {endpoint}: {raw[:200]}") from exc

    status = parsed.get("status")
    if status not in (None, 0, 1, 200):
        message = parsed.get("message") or parsed.get("msg") or parsed
        raise ProtocolError(f"API {endpoint} failed with status {status}: {message}")
    return parsed


def api_login(email: str, password: str) -> str:
    data = api_post(
        "appHeyApi/login",
        {
            "userEmail": email,
            "phoneInfo": APP_VERSION,
            "phoneType": PHONE_TYPE,
            "phoneSystems": PHONE_SYSTEMS,
            "userPass": password,
        },
    )
    token = str(data.get("token") or "")
    if not token:
        raise ProtocolError(f"login succeeded but no token was returned: {data}")
    return token


def api_user_bikes(token: str) -> list[AccountBike]:
    data = api_post("appHeyApi/getUserBikes", {"token": token})
    bikes_raw = data.get("bikes")
    if not isinstance(bikes_raw, list):
        bikes_raw = []
    bikes: list[AccountBike] = []
    for item in bikes_raw:
        if not isinstance(item, dict):
            continue
        mac = str(item.get("deBle") or item.get("bleMac") or item.get("macAddress") or "")
        ble_key = str(item.get("bleKey") or "")
        if not mac or not ble_key:
            continue
        name = str(item.get("nickName") or item.get("name") or item.get("typeName") or "")
        if not name:
            bike_type = item.get("bikeType")
            if isinstance(bike_type, dict):
                name = str(bike_type.get("typeName") or "")
        if not name:
            name = normalize_mac(mac)
        bikes.append(
            AccountBike(
                name=name,
                mac=normalize_mac(mac),
                encrypted_ble_key=ble_key,
                imei=str(item.get("deIMEI") or ""),
                raw=item,
            )
        )
    return bikes


def api_bike_by_ble_mac(token: str, mac: str) -> AccountBike:
    data = api_post("appHeyApi/getBikeByBleMac", {"token": token, "deBle": normalize_mac(mac)})
    bike = data.get("bike")
    if not isinstance(bike, dict):
        raise ProtocolError(f"getBikeByBleMac returned no bike: {data}")
    ble_key = str(bike.get("bleKey") or bike.get("tBleKey") or "")
    if not ble_key:
        raise ProtocolError(f"getBikeByBleMac returned no bleKey: {data}")
    return AccountBike(
        name=str(bike.get("bleName") or bike.get("tBleName") or normalize_mac(mac)),
        mac=normalize_mac(mac),
        encrypted_ble_key=ble_key,
        imei=str(bike.get("deIMEI") or bike.get("imei") or ""),
        raw=bike,
    )


def choose_account_bike(
    bikes: list[AccountBike],
    *,
    requested_mac: Optional[str] = None,
    requested_name: Optional[str] = None,
    requested_index: Optional[int] = None,
    non_interactive: bool = False,
) -> AccountBike:
    if requested_mac:
        wanted = compact_mac(requested_mac)
        matches = [bike for bike in bikes if compact_mac(bike.mac) == wanted]
        if not matches:
            raise ProtocolError(f"no account bike has MAC {requested_mac}")
        return matches[0]

    if requested_name:
        wanted_name = requested_name.lower()
        matches = [bike for bike in bikes if wanted_name in bike.name.lower()]
        if not matches:
            raise ProtocolError(f"no account bike name contains {requested_name!r}")
        if len(matches) == 1:
            return matches[0]
        bikes = matches

    if requested_index is not None:
        if requested_index < 0 or requested_index >= len(bikes):
            raise ProtocolError(f"--bike-index must be between 0 and {len(bikes) - 1}")
        return bikes[requested_index]

    if len(bikes) == 1:
        return bikes[0]
    if not bikes:
        raise ProtocolError("no bikes were returned by the account")
    if non_interactive:
        raise ProtocolError("multiple bikes found; pass --bike-index, --bike-name, or --bike-mac")

    print("Account bikes:")
    for index, bike in enumerate(bikes):
        extra = f"  IMEI {bike.imei}" if bike.imei else ""
        print(f"  [{index}] {bike.name}  {bike.mac}{extra}")
    choice = input("Select bike index: ").strip()
    try:
        index = int(choice)
    except ValueError as exc:
        raise ProtocolError(f"invalid bike index {choice!r}") from exc
    if index < 0 or index >= len(bikes):
        raise ProtocolError(f"bike index out of range: {index}")
    return bikes[index]


def build_power_frame(turn_on: bool, token: bytes) -> bytes:
    if len(token) != 4:
        raise ValueError("token must be exactly 4 bytes")
    frame = bytearray(16)
    frame[0] = 0x61
    frame[1] = 0x62
    frame[2] = POWER_OPCODE
    frame[3] = 1
    frame[4] = 1 if turn_on else 0
    frame[12:16] = token
    return bytes(frame)


def decrypt_notification(
    data: bytes,
    ble_key: str,
    crypto: CryptoConfig,
) -> Optional[bytes]:
    if len(data) != 18 or data[0] != 0x7B or data[-1] != 0x7D:
        return None
    encrypted = data[1:-1]
    return aes_crypt(
        encrypted,
        ble_key,
        crypto.command_transformation,
        decrypt=True,
    )


async def scan_bike_devices(scan_seconds: float, service_uuid: str = SERVICE_UUID):
    print(f"Scanning {scan_seconds:.1f}s for Heybike BLE devices...")
    devices = await BleakScanner.discover(timeout=scan_seconds)
    bikes = []
    for dev in devices:
        service_uuids = device_service_uuids(dev)
        has_service = service_uuid.lower() in service_uuids
        if has_service or is_heybike_name(dev.name):
            bikes.append(dev)
    bikes.sort(key=device_rssi, reverse=True)
    return bikes


async def resolve_device(
    address: Optional[str],
    name: Optional[str],
    scan_seconds: float,
    *,
    account_bike: Optional[AccountBike] = None,
    non_interactive: bool = False,
    service_uuid: str = SERVICE_UUID,
):
    if address:
        return normalize_mac(address)

    if account_bike:
        account_mac = compact_mac(account_bike.mac)
        bikes = await scan_bike_devices(scan_seconds, service_uuid)
        matches = [dev for dev in bikes if compact_mac(dev.address) == account_mac]
        if matches:
            dev = matches[0]
            print(f"Matched account bike: {dev.address}  {dev.name or ''}")
            return dev.address
        if len(bikes) == 1:
            dev = bikes[0]
            print(
                "No scan address matched account MAC; using only visible Heybike "
                f"device: {dev.address}  {dev.name or ''}"
            )
            return dev.address
        if bikes:
            print(f"No scanned device matched account MAC {account_bike.mac}.")
            print("Visible Heybike devices:")
            for index, dev in enumerate(bikes):
                print(f"  [{index}] {dev.address}  {dev.name or ''}  RSSI {device_rssi(dev)}")
            if non_interactive:
                raise ProtocolError("multiple visible bikes; pass --address")
            choice = input("Select BLE device index, or blank to use account MAC directly: ").strip()
            if choice:
                try:
                    index = int(choice)
                except ValueError as exc:
                    raise ProtocolError(f"invalid BLE device index {choice!r}") from exc
                if index < 0 or index >= len(bikes):
                    raise ProtocolError(f"BLE device index out of range: {index}")
                return bikes[index].address
        print(f"No visible Heybike device found; trying account MAC {account_bike.mac}")
        return account_bike.mac

    if name:
        print(f"Scanning {scan_seconds:.1f}s for device name containing {name!r}...")
        devices = await BleakScanner.discover(timeout=scan_seconds)
        matches = [
            dev for dev in devices if dev.name and name.lower() in dev.name.lower()
        ]
    else:
        matches = await scan_bike_devices(scan_seconds, service_uuid)
    if not matches:
        raise ProtocolError("no matching Heybike BLE device found")
    if len(matches) > 1:
        print("Multiple matches:")
        for index, dev in enumerate(matches):
            print(f"  [{index}] {dev.address}  {dev.name or ''}  RSSI {device_rssi(dev)}")
        if non_interactive:
            raise ProtocolError("rerun with --address for the exact bike")
        choice = input("Select BLE device index: ").strip()
        try:
            index = int(choice)
        except ValueError as exc:
            raise ProtocolError(f"invalid BLE device index {choice!r}") from exc
        if index < 0 or index >= len(matches):
            raise ProtocolError(f"BLE device index out of range: {index}")
        dev = matches[index]
        print(f"Using {dev.address}  {dev.name or ''}")
        return dev.address
    dev = matches[0]
    print(f"Found {dev.address}  {dev.name or ''}")
    return dev.address


async def run_power_command(args: argparse.Namespace) -> int:
    crypto = CryptoConfig(
        command_transformation=args.command_transformation,
        key_transformation=args.key_transformation,
    )

    account_bike: Optional[AccountBike] = None
    encrypted_key = args.encrypted_key

    if args.email or args.api_token:
        api_token = args.api_token
        if not api_token:
            password = args.password
            if password is None:
                password = getpass.getpass(f"HeyBike password for {args.email}: ")
            print("Logging in to HeyBike API...")
            api_token = api_login(args.email, password)
        print("Fetching account bikes...")
        account_bikes = api_user_bikes(api_token)
        account_bike = choose_account_bike(
            account_bikes,
            requested_mac=args.bike_mac,
            requested_name=args.bike_name,
            requested_index=args.bike_index,
            non_interactive=args.non_interactive,
        )
        print(f"Selected bike: {account_bike.name}  {account_bike.mac}")
        encrypted_key = account_bike.encrypted_ble_key

    if args.key:
        ble_key = args.key
    elif encrypted_key:
        native_key_secret = args.native_key_secret or NATIVE_KEY_SECRET
        ble_key = decrypt_server_ble_key(
            encrypted_key,
            native_key_secret,
            crypto.key_transformation,
        )
    else:
        raise ValueError("provide --email/--password, --api-token, --key, or --encrypted-key")

    token_fetch_encrypted = aes_crypt(
        TOKEN_FETCH_CLEAR,
        ble_key,
        crypto.command_transformation,
        decrypt=False,
    )

    turn_on = args.state == "on"
    supplied_token = parse_hex_bytes(args.token, 4) if args.token else None

    if args.dry_run:
        print(f"BLE key length: {len(ble_key.encode('utf-8'))} bytes")
        print(f"Token fetch clear:     {hx(TOKEN_FETCH_CLEAR)}")
        print(f"Token fetch encrypted: {hx(token_fetch_encrypted)}")
        if supplied_token:
            power_clear = build_power_frame(turn_on, supplied_token)
            power_encrypted = aes_crypt(
                power_clear,
                ble_key,
                crypto.command_transformation,
                decrypt=False,
            )
            print(f"Power clear:           {hx(power_clear)}")
            print(f"Power encrypted:       {hx(power_encrypted)}")
        else:
            print("Power frame needs a live token; pass --token to dry-run it.")
        return 0

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
    power_future: asyncio.Future[bytes] = loop.create_future()

    def on_notify(_: int, data: bytearray):
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
            if args.verbose:
                print(f"notify ignored: {hx(raw)}")
            return
        if args.verbose:
            print(f"notify clear: {hx(clear)}")

        if clear[:3] == TOKEN_PREFIX and not token_future.done():
            loop.call_soon_threadsafe(token_future.set_result, clear[4:8])
            return

        if len(clear) >= 5 and clear[2] == POWER_OPCODE and not power_future.done():
            loop.call_soon_threadsafe(power_future.set_result, clear)

    async with BleakClient(address, timeout=args.connect_timeout) as client:
        if not client.is_connected:
            raise ProtocolError("BLE connection failed")
        print(f"Connected to {address}")
        await client.start_notify(args.notify_uuid, on_notify)
        await asyncio.sleep(0.2)

        if supplied_token is None:
            print("Fetching live token...")
            if args.verbose:
                print(f"write token encrypted: {hx(token_fetch_encrypted)}")
            await client.write_gatt_char(
                args.write_uuid,
                token_fetch_encrypted,
                response=not args.no_response,
            )
            token = await asyncio.wait_for(token_future, timeout=args.timeout)
            print(f"Token: {hx(token)}")
        else:
            token = supplied_token
            print(f"Using supplied token: {hx(token)}")

        power_clear = build_power_frame(turn_on, token)
        power_encrypted = aes_crypt(
            power_clear,
            ble_key,
            crypto.command_transformation,
            decrypt=False,
        )
        print(f"Sending power {'on' if turn_on else 'off'}...")
        if args.verbose:
            print(f"write power clear:     {hx(power_clear)}")
            print(f"write power encrypted: {hx(power_encrypted)}")
        await client.write_gatt_char(
            args.write_uuid,
            power_encrypted,
            response=not args.no_response,
        )
        response = await asyncio.wait_for(power_future, timeout=args.timeout)
        status = response[4] if len(response) > 4 else None
        print(f"Power response: {hx(response)}")
        if status == (1 if turn_on else 0):
            print("Bike reported requested power state.")
        else:
            print(f"Bike response status byte was {status!r}; verify state manually.")

        await client.stop_notify(args.notify_uuid)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Turn a HeyBike/YuLai bike on or off over BLE."
    )
    parser.add_argument("state", choices=("on", "off"))
    parser.add_argument("--email", help="HeyBike account email. Enables automatic bike/key lookup.")
    parser.add_argument("--password", help="HeyBike account password. If omitted with --email, prompts securely.")
    parser.add_argument("--api-token", help="Existing HeyBike API token; skips login and fetches account bikes.")
    parser.add_argument("--bike-index", type=int, help="Account bike index to use when multiple bikes are returned.")
    parser.add_argument("--bike-name", help="Account bike name substring to select.")
    parser.add_argument("--bike-mac", help="Account bike BLE MAC to select.")
    parser.add_argument("--address", help="BLE address/MAC. Usually unnecessary with --email/--api-token.")
    parser.add_argument("--name", help="Scan for a BLE device whose name contains this text.")
    parser.add_argument("--scan-seconds", type=float, default=8.0)
    parser.add_argument(
        "--key",
        help="Transformed/plain BLE command key, after the app's vl.a.b(bleKey) step.",
    )
    parser.add_argument(
        "--encrypted-key",
        help="Server-provided bleKey from the app/API.",
    )
    parser.add_argument(
        "--native-key-secret",
        default=NATIVE_KEY_SECRET,
        help="NativeLib.getKeySecret() value in KEY:IV form.",
    )
    parser.add_argument(
        "--command-transformation",
        default=NATIVE_COMMAND_TRANSFORMATION,
        help="NativeLib.getCryptTransformation().",
    )
    parser.add_argument(
        "--key-transformation",
        default=NATIVE_KEY_TRANSFORMATION,
        help="NativeLib.getCryptTransformation2(); used only with --encrypted-key.",
    )
    parser.add_argument("--service-uuid", default=SERVICE_UUID)
    parser.add_argument("--write-uuid", default=WRITE_UUID)
    parser.add_argument("--notify-uuid", default=NOTIFY_UUID)
    parser.add_argument("--token", help="Optional live 4-byte token as hex; skips token fetch.")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--no-response", action="store_true", help="Use BLE write-without-response.")
    parser.add_argument("--dry-run", action="store_true", help="Print frames without connecting.")
    parser.add_argument("--non-interactive", action="store_true", help="Fail instead of prompting for bike selection.")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(run_power_command(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
