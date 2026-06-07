from __future__ import annotations

import asyncio
import base64
import json
import platform

from dataclasses import dataclass
from enum import IntEnum
from functools import cache
from typing import Any, AsyncIterator, Optional
from urllib import parse, request

from bleak import AdvertisementData, BleakClient, BleakScanner, BLEDevice
from Crypto.Cipher import AES

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
COMMAND_PREFIX = b"ab"
NOTIFICATION_START = 0x7B
NOTIFICATION_END = 0x7D
TOKEN_PREFIX = bytes([0x16, 0x5A, 0x01])
TOKEN_FETCH_CLEAR = TOKEN_PREFIX + bytes(13)


class OpCode(IntEnum):
    POWER = 0x31
    BASE_INFO = 0x38


@dataclass
class NearbyHeybike:
    mac: str
    name: str


@dataclass(frozen=True)
class BaseInfo:
    error_code: int
    battery_percent: int
    auto_lock_enabled: bool
    power_on: bool
    hardware_version: int
    iot_firmware_version: int
    protocol_version: int


def compact_mac(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch in "0123456789ABCDEF")


def normalize_mac(value: str) -> str:
    compact = compact_mac(value)
    if len(compact) == 12:
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))
    return value.upper()


def _pkcs7_unpad(data: bytes, block_size: int = AES.block_size) -> bytes:
    if not data or len(data) % block_size:
        raise RuntimeError("invalid padded data length")

    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        raise RuntimeError("invalid padding length")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise RuntimeError("invalid padding bytes")
    return data[:-pad_len]


def _aes_key(key_text: str) -> bytes:
    key = key_text.encode("utf-8")
    if len(key) not in (16, 24, 32):
        raise RuntimeError(f"AES key must be 16, 24, or 32 UTF-8 bytes; got {len(key)}")
    return key


def decrypt_server_ble_key(encrypted_ble_key: str, native_key_secret: str = NATIVE_KEY_SECRET) -> str:
    """Decrypt a server/API `bleKey` into the BLE command key used on the bike."""

    try:
        key_text, iv_text = native_key_secret.split(":", 1)
    except ValueError as exc:
        raise RuntimeError("native key secret must be in KEY:IV form") from exc

    iv = iv_text.encode("utf-8")
    if len(iv) != AES.block_size:
        raise RuntimeError(f"AES-CBC IV must be {AES.block_size} UTF-8 bytes; got {len(iv)}")

    try:
        encrypted = base64.b64decode(encrypted_ble_key, validate=True)
    except ValueError as exc:
        raise RuntimeError("server BLE key is not valid base64") from exc

    clear = AES.new(_aes_key(key_text), AES.MODE_CBC, iv=iv).decrypt(encrypted)
    return _pkcs7_unpad(clear).decode("utf-8").strip()


def _encrypt_ble_frame(clear: bytes, ble_key: str) -> bytes:
    if len(clear) != AES.block_size:
        raise RuntimeError(f"BLE command frames must be {AES.block_size} bytes")
    return AES.new(_aes_key(ble_key), AES.MODE_ECB).encrypt(clear)


def decrypt_ble_notification(data: bytes, ble_key: str) -> bytes | None:
    """Return the clear 16-byte notification frame, or `None` for unrelated data."""

    if len(data) != AES.block_size + 2 or data[0] != NOTIFICATION_START or data[-1] != NOTIFICATION_END:
        return None
    encrypted = data[1:-1]
    return AES.new(_aes_key(ble_key), AES.MODE_ECB).decrypt(encrypted)


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

    def enqueue_once(record: NearbyHeybike) -> None:
        key = record.mac
        if key in seen:
            return
        seen.add(key)
        queue.put_nowait(record)

    def on_advertisement(device: BLEDevice, advertisement_data: AdvertisementData) -> None:
        name = advertisement_data.local_name or device.name
        has_matching_name = bool(name) and name.lower().startswith(wanted_prefix)
        has_matching_service = wanted_service and wanted_service in advertisement_data.service_uuids
        if not has_matching_name and not has_matching_service:
            return

        if device.address:
            record = NearbyHeybike(
                mac=normalize_mac(device.address),
                name=name,
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


def api_post(endpoint: str, fields: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    body = parse.urlencode(fields).encode("utf-8")
    req = request.Request(
        parse.urljoin(API_BASE_URL, endpoint),
        data=body,
        headers={
            "systemtype": "android",
            "phoneInfo": APP_VERSION,
            "phoneType": PHONE_TYPE,
            "phoneSystems": PHONE_SYSTEMS,
            "source": "1",
            "language": LANGUAGE,
            "countryCode": COUNTRY_CODE,
            "User-Agent": "okhttp/4.11.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API returned non-JSON from {endpoint}: {raw[:200]}") from exc

    status = parsed.get("status")
    if status not in (None, 0, 1, 200):
        message = parsed.get("message") or parsed.get("msg") or parsed
        raise RuntimeError(f"API {endpoint} failed with status {status}: {message}")
    return parsed


@dataclass
class Heybike:
    mac: str
    name: str = ""
    email: str | None = None
    password: str | None = None
    token: str | None = None
    _imei: str | None = None
    _ble_key: str | None = None
    _ble_token: bytes | None = None
    _hardware_version: int | None = None
    _iot_firmware_version: int | None = None
    _protocol_version: int | None = None

    @classmethod
    def account_bikes(
        cls,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
        nearby: bool = False,
    ):
        if token is None:
            if email is None or password is None:
                raise ValueError("Account information is needed to get bikes associated with an account")
            token = cls._login(email, password)

        data = api_post("appHeyApi/getUserBikes", {"token": token})
        bikes_raw = data.get("bikes")
        if not isinstance(bikes_raw, list):
            bikes_raw = []

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

            # TODO: Is nearby? Get list of all nearby_bikes below (owned=False) and cross reference.

            yield Heybike(
                name=name,
                mac=normalize_mac(mac),
                email=email,
                password=password,
                token=token,
                _ble_key=decrypt_server_ble_key(ble_key),
                _imei=str(item.get("deIMEI") or ""),
            )

    @classmethod
    async def nearby_bikes(
        cls,
        scan_seconds: float = 20,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
        owned: bool = False,
    ) -> AsyncIterator[Heybike]:
        owned_bikes = set()
        if owned:
            if (email is None or password is None) and token is None:
                raise ValueError("Account information is needed to get nearby bikes associated with an account")

            if token is None:
                token = cls._login(email, password)

            owned_bikes = set(cls.account_bikes(email=email, password=password, token=token, nearby=False))

        async for bike in iter_nearby_heybikes(scan_seconds=scan_seconds):
            found_bike = Heybike(mac=bike.mac, name=bike.name, email=email, password=password, token=token)

            if owned and found_bike not in owned_bikes:
                continue

            yield found_bike

    def __hash__(self):
        return hash(self.mac)

    def __eq__(self, other):
        return isinstance(other, Heybike) and self.mac == other.mac

    @staticmethod
    @cache
    def _login(email: str, password: str) -> str:
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
            raise RuntimeError(f"login succeeded but no token was returned: {data}")
        return token

    def _fill_bike_info(
        self,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ):
        """
        Ensure that bike info, particularly the BLE key, are present.
            Get them from getBikeByBleMac if not (requires credentials to be present)

        Args:
            email: The email to use, otherwise self.email. Only needed if a token is not present.
            password: The password to use, otherwise self.password.
            token: The token to use. If not provided, and not in self.token, one will be generated from credentials.
        """
        if self._ble_key:
            return  # Bike info already gotten

        if email is not None:
            self.email = email
        if password is not None:
            self.password = password
        if token is not None:
            self.token = token

        token = self.token
        if token is None:
            if self.email is not None and self.password is not None:
                token = self._login(self.email, self.password)
                self.token = token
            else:
                # TODO: Critically this API does NOT authenticate that the associated bike belongs to this account
                raise ValueError("Account information is needed to get complete bike information.")

        data = api_post("appHeyApi/getBikeByBleMac", {"token": token, "deBle": normalize_mac(self.mac)})
        bike = data.get("bike")
        if not isinstance(bike, dict):
            raise RuntimeError(f"getBikeByBleMac returned no bike: {data}")
        ble_key = str(bike.get("bleKey") or bike.get("tBleKey") or "")
        if not ble_key:
            raise RuntimeError(f"getBikeByBleMac returned no bleKey: {data}")

        self._ble_key = decrypt_server_ble_key(ble_key)
        self._imei = str(bike.get("deIMEI") or bike.get("imei") or "")
        self.name = str(bike.get("bleName") or bike.get("tBleName") or "")

    @property
    def ble_key(self) -> str:
        self._fill_bike_info()
        if self._ble_key is None:
            raise RuntimeError("bike BLE key is unavailable")
        return self._ble_key

    async def _fetch_ble_token(
        self,
        client: BleakClient,
        token_future: asyncio.Future[bytes],
        *,
        ble_key: str,
        write_uuid: str,
        timeout: float,
        write_with_response: bool,
    ) -> bytes:
        if self._ble_token:
            return self._ble_token

        token_fetch_encrypted = _encrypt_ble_frame(TOKEN_FETCH_CLEAR, ble_key)
        await client.write_gatt_char(write_uuid, token_fetch_encrypted, response=write_with_response)
        self._ble_token = await asyncio.wait_for(token_future, timeout=timeout)
        return self._ble_token

    async def send_ble_command(
        self,
        opcode: OpCode,
        payload: bytes = b"",
        *,
        timeout: float = 10.0,
        connect_timeout: float = 20.0,
        write_uuid: str = WRITE_UUID,
        notify_uuid: str = NOTIFY_UUID,
        write_with_response: bool = True,
    ) -> bytes:
        """Send one encrypted BLE command and return the matching clear response frame."""

        ble_key = self.ble_key
        payload = bytes(payload)
        loop = asyncio.get_running_loop()
        token_future: asyncio.Future[bytes] = loop.create_future()
        response_future: asyncio.Future[bytes] = loop.create_future()

        def on_notify(_: Any, data: bytearray) -> None:
            clear = decrypt_ble_notification(bytes(data), ble_key)
            if clear is None:
                return
            if clear[:3] == TOKEN_PREFIX and not token_future.done():
                loop.call_soon_threadsafe(token_future.set_result, clear[4:8])
                return
            if clear[0:2] == COMMAND_PREFIX and clear[2] == opcode.value and not response_future.done():
                loop.call_soon_threadsafe(response_future.set_result, clear)

        async with BleakClient(self.mac, timeout=connect_timeout) as client:
            if not client.is_connected:
                raise RuntimeError(f"BLE connection failed for {self.mac}")

            await client.start_notify(notify_uuid, on_notify)
            try:
                await asyncio.sleep(0.2)
                token = await self._fetch_ble_token(
                    client,
                    token_future,
                    ble_key=ble_key,
                    write_uuid=write_uuid,
                    timeout=timeout,
                    write_with_response=write_with_response,
                )

                # Build BLE command frame:
                if not 0 <= opcode.value <= 0xFF:
                    raise ValueError("opcode must fit in one byte")
                if len(payload) > 8:
                    raise ValueError("payload must fit in bytes 4..11")
                if len(token) != 4:
                    raise ValueError("token must be exactly 4 bytes")

                frame = bytearray(AES.block_size)
                frame[0:2] = COMMAND_PREFIX
                frame[2] = opcode.value
                frame[3] = len(payload)
                frame[4 : 4 + len(payload)] = payload
                frame[12:16] = token

                encrypted = _encrypt_ble_frame(bytes(frame), ble_key)
                await client.write_gatt_char(write_uuid, encrypted, response=write_with_response)
                return await asyncio.wait_for(response_future, timeout=timeout)
            finally:
                await client.stop_notify(notify_uuid)

    async def get_base_info(self) -> BaseInfo:
        response = await self.send_ble_command(OpCode.BASE_INFO)
        if len(response) <= 10:
            raise RuntimeError("base-info response did not include all expected fields")

        base_info = BaseInfo(
            error_code=response[4],
            battery_percent=response[5],
            auto_lock_enabled=bool(response[6]),
            power_on=bool(response[7]),
            hardware_version=response[8],
            iot_firmware_version=response[9],
            protocol_version=response[10],
        )
        self._hardware_version = base_info.hardware_version
        self._iot_firmware_version = base_info.iot_firmware_version
        self._protocol_version = base_info.protocol_version
        return base_info

    async def get_hardware_version(self) -> int:
        if self._hardware_version is None:
            await self.get_base_info()

        return self._hardware_version

    async def iot_firmware_version(self) -> int:
        if self._iot_firmware_version is None:
            await self.get_base_info()

        return self._iot_firmware_version

    async def protocol_version(self) -> int | None:
        if self._protocol_version is None:
            await self.get_base_info()

        return self._protocol_version

    async def get_battery_percent(self) -> int:
        return (await self.get_base_info()).battery_percent

    async def get_auto_lock(self) -> bool:
        return (await self.get_base_info()).auto_lock_enabled

    async def get_power(self) -> bool:
        return (await self.get_base_info()).power_on

    async def set_power(self, value: bool) -> None:
        """Set the bike power state."""

        requested = bool(value)
        response = await self.send_ble_command(OpCode.POWER, bytes([1 if requested else 0]))
        if len(response) <= 4:
            raise RuntimeError("power response did not include the status byte")

        reported = bool(response[4])
        if reported != requested:
            raise RuntimeError(f"bike reported power={reported}, expected power={requested}")

    async def toggle_power(self) -> bool:
        new_power = not await self.get_power()
        await self.set_power(new_power)
        return new_power
