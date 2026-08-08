from __future__ import annotations

import asyncio
import base64
import csv
import datetime as dt
import json
import logging
import os

from collections import defaultdict
from dataclasses import dataclass, field, replace
from enum import IntEnum
from functools import cache
from pathlib import Path
from typing import Any, AsyncIterator, Callable, ClassVar
from urllib import parse, request

from bleak import AdvertisementData, BleakClient, BleakScanner, BLEDevice
from Crypto.Cipher import AES

from . import settings
from .firmware import (
    YMODEM_CONTROL_BYTES,
    FirmwareUpdate,
    FirmwareUpdateError,
    YModem128,
    firmware_mode_and_name,
)

logger = logging.getLogger(__name__)

COMMAND_PREFIX = b"ab"
NOTIFICATION_START = 0x7B
NOTIFICATION_END = 0x7D
TOKEN_PREFIX = bytes([0x16, 0x5A, 0x01])
MPH_FACTOR = 0.62137
BLE_KEY_CACHE_FIELDS = ("mac", "ble_key", "imei", "name")


class HeybikeApiError(RuntimeError):
    """Raised when a HeyBike API endpoint returns an error status."""

    def __init__(self, endpoint: str, status, message, payload: dict[str, Any]):  # ruff: ignore[missing-type-function-argument]
        """Simply establish the variables"""
        self.endpoint = endpoint
        self.status = status
        self.message = message
        self.payload = payload
        super().__init__(f"API {endpoint} failed with status {status}: {message}")


class OpCode(IntEnum):
    """BLE op codes for interacting with the bike."""

    ICCID_FIRST = 0x20
    ICCID_SECOND = 0x21
    ICCID_FINAL = 0x22

    POWER = 0x31
    RESET_TO_DEFAULT = 0x32
    RESET_TRIP_DISTANCE = 0x34
    OTA_START = 0x35
    SYNC_TIME = 0x36
    BASE_INFO = 0x38
    MILEAGE = 0x39
    HEADLIGHT = 0x42
    RIDE_FEEL = 0x44
    THROTTLE_SENSITIVITY = 0x49
    IMEI_FIRST = 0xA0
    IMEI_SECOND = 0xA1
    SIGNAL_GPS = 0xD1
    ANTI_THEFT = 0xD7
    AUTO_LOCK = 0xD8
    HANDLE_PWM = 0xDA
    HANDLE_GEAR = 0xDB
    SPEED_LIMITER_TYPE = 0xDC
    PRESET_MODE = 0xDF
    MAX_SPEED = 0xE1
    SPEED_UNIT = 0xE5
    VOLTAGE = 0xE6
    DRIVE_GEAR = 0xE7
    START_GEAR = 0xEA
    BACKLIGHT_BRIGHTNESS = 0xEF


@dataclass(frozen=True)
class BaseInfo:
    """Base info data structure containing all info returned by the get base info op code."""
    error_code: int
    battery_percent: int
    auto_lock_enabled: bool
    power_on: bool
    hardware_version: int
    iot_firmware_version: int
    protocol_version: int


@dataclass(frozen=True)
class SignalGpsInfo:
    """Signal and GPS strength returned by the bike."""
    signal_intensity: int
    gps_signal: int


@dataclass(frozen=True)
class AntiTheftInfo:
    """Anti-theft/fence status returned by the bike."""
    enabled: bool
    distance: int


@dataclass(frozen=True)
class AutoLockInfo:
    """Auto-lock status and timeout returned by the bike."""
    enabled: bool
    time: int


@dataclass(frozen=True)
class CachedBikeInfo:
    """Bike info stored in the cache file"""
    mac: str
    ble_key: str
    imei: str = ""
    name: str = ""


def _int(value: str | None) -> int | None:
    if value is None or value == "":  # ruff: ignore[compare-to-empty-string]
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class BikeColorInfo:
    """Color metadata returned by `getAllBikeColorType` or bike account APIs."""

    de_type: int | None
    color_id: int | None
    name: str = ""
    values: tuple[str, ...] = ()
    raw_value: str = ""
    big_image_url: str = ""
    small_image_url: str = ""
    side_image_url: str = ""
    added_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> BikeColorInfo:
        """Convert API data to BikeColorInfo."""
        explicit_values = tuple(
            value
            for value in (
                data.get("cvalue1"),
                data.get("cvalue2"),
                data.get("cvalue3"),
            )
            if value
        )
        raw_value = data.get("cvalue", "")
        if not explicit_values:
            explicit_values = [value for part in raw_value.split(";") if (value := part.strip())]

        return cls(
            de_type=_int(data.get("deType")),
            color_id=_int(data.get("coId")),
            name=data.get("cname", ""),
            values=explicit_values,
            raw_value=raw_value,
            big_image_url=data.get("bigImg", ""),
            small_image_url=data.get("smallImg", ""),
            side_image_url=data.get("sideImg", ""),
            added_at=data.get("addTime", ""),
            raw=dict(data),
        )

    @property
    def primary_value(self) -> str:
        """The first color value, or an empty string if none is present."""
        return self.values[0] if self.values else ""


@dataclass(frozen=True)
class BikeModelInfo:
    """Bike model metadata returned by `getAllBikeType` or bike account APIs."""

    id: int | None
    name: str = ""
    max_speed: int | None = None
    initial_max_speed: int | None = None
    gear_num: int | None = None
    battery_voltage: int | None = None
    battery_capacity: int | None = None
    speed_unit: int | None = None
    vehicle_category: int | None = None
    big_image_url: str = ""
    small_image_url: str = ""
    side_image_url: str = ""
    instruction_url: str = ""
    colors: list[BikeColorInfo] = field(default_factory=dict, compare=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(
        cls,
        data: dict[str, Any],
        *,
        colors: list[BikeColorInfo] | None = None,
    ) -> BikeModelInfo:
        """Convert API data to BikeModelInfo."""
        return cls(
            id=_int(data.get("typeId") or data.get("id")),
            name=str(data.get("typeName") or data.get("bikeType")),
            max_speed=_int(data.get("maxSpeed")),
            initial_max_speed=_int(data.get("initialMaxSpeed")),
            gear_num=_int(data.get("gearNum")),
            battery_voltage=_int(data.get("batteryVoltage")),
            battery_capacity=_int(data.get("batteryCapacity")),
            speed_unit=_int(data.get("speedUnit")),
            vehicle_category=_int(
                data.get("vehicleCategory") or data.get("vehicleCategoryId") or data.get("bikeCategory"),
            ),
            big_image_url=data.get("bigImg", ""),
            small_image_url=data.get("smallImg", ""),
            side_image_url=data.get("sideImg", ""),
            instruction_url=data.get("instructionUrl", ""),
            colors=colors or [],
            raw=dict(data),
        )

    @property
    def speed_unit_name(self) -> str | None:
        """The app label for the default speed unit, when known."""
        if self.speed_unit == 0:
            return "km"
        if self.speed_unit == 1:
            return "mile"
        return None


@dataclass(frozen=True)
class BikeIdentityInfo:
    """Model/color metadata returned for one IMEI when the server permits it."""

    imei: str
    model: BikeModelInfo | None = None
    color: BikeColorInfo | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, imei: str, data: dict[str, Any]) -> BikeIdentityInfo:
        """Convert API data to BikeIdentityInfo."""
        bike = data.get("bike", {})
        model_data = data.get("bikeType") or bike.get("bikeType")
        color_data = data.get("bikeColor") or bike.get("bikeColor")
        return cls(
            imei=bike.get("deIMEI") or imei,
            model=BikeModelInfo.from_api(model_data) if model_data else None,
            color=BikeColorInfo.from_api(color_data) if color_data else None,
            raw=dict(data),
        )


def normalize_mac(value: str) -> str:
    """Normalize a MAC value, whether from bleak or the API."""
    compact = "".join(ch for ch in value.upper() if ch in "0123456789ABCDEF")
    if len(compact) == 12:
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))
    return value.upper()


def _ascii_clean(data: bytes) -> str:
    return bytes(byte for byte in data if byte).decode("utf-8", errors="replace")


def _u16be(data: bytes, offset: int = 4) -> int:
    if len(data) <= offset + 1:
        raise RuntimeError("response did not include a two-byte value")
    return (data[offset] << 8) | data[offset + 1]


def _u16be_payload(value: int, name: str) -> bytes:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    if value > 0xFFFF:
        raise ValueError(f"{name} raw BLE value must fit in two bytes")
    return value.to_bytes(2, "big")


def _read_ble_key_cache(path: str | Path | None, mac: str) -> CachedBikeInfo | None:
    if path is None:
        return None

    path = Path(path)

    if not path.exists():
        return None

    normalized_mac = normalize_mac(mac)
    try:
        with path.open("r", encoding="utf-8", newline="") as cache_file:
            for row in csv.DictReader(cache_file):
                if normalize_mac(row.get("mac", "")) != normalized_mac:
                    continue
                ble_key = (row.get("ble_key") or "").strip()
                if ble_key:
                    return CachedBikeInfo(
                        mac=normalized_mac,
                        ble_key=ble_key,
                        imei=(row.get("imei") or "").strip(),
                        name=(row.get("name") or "").strip(),
                    )
    except (OSError, csv.Error) as exc:
        logger.warning("Could not read Heybike BLE key cache %s: %s", path, exc)

    return None


def _write_ble_key_cache(
    path: str | Path | None,
    *,
    mac: str,
    ble_key: str,
    imei: str = "",
    name: str = "",
):
    if path is None:
        return

    path = Path(path)

    normalized_mac = normalize_mac(mac)
    record = {
        "mac": normalized_mac,
        "ble_key": ble_key,
        "imei": imei,
        "name": name,
    }
    rows: list[dict[str, str]] = []
    fieldnames = list(BLE_KEY_CACHE_FIELDS)

    try:
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as cache_file:
                reader = csv.DictReader(cache_file)
                if reader.fieldnames:
                    fieldnames = list(reader.fieldnames)
                rows = [dict(row) for row in reader]

        for field in BLE_KEY_CACHE_FIELDS:
            if field not in fieldnames:
                fieldnames.append(field)

        updated = False
        for row in rows:
            if normalize_mac(row.get("mac", "")) == normalized_mac:
                row.update(record)
                updated = True

        if not updated:
            rows.append(record)

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as cache_file:
            writer = csv.DictWriter(cache_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except (OSError, csv.Error) as exc:
        logger.warning("Could not write Heybike BLE key cache %s: %s", path, exc)


def _set_future_result(future: asyncio.Future[bytes], value: bytes):
    if not future.done():
        future.set_result(value)


def _build_ble_command_frame(opcode: int, payload: bytes = b"", token: bytes = bytes(4)) -> bytes:
    if not 0 <= opcode <= 0xFF:
        raise ValueError("opcode must fit in one byte")
    if len(payload) > 8:
        raise ValueError("payload must fit in bytes 4..11")
    if len(token) != 4:
        raise ValueError("token must be exactly 4 bytes")

    frame = bytearray(AES.block_size)
    frame[0:2] = COMMAND_PREFIX
    frame[2] = opcode
    frame[3] = len(payload)
    frame[4 : 4 + len(payload)] = payload
    frame[12:16] = token
    return bytes(frame)


def _aes_key(key_text: str) -> bytes:
    key = key_text.encode("utf-8")
    if len(key) not in (16, 24, 32):
        raise RuntimeError(f"AES key must be 16, 24, or 32 UTF-8 bytes; got {len(key)}")
    return key


def decrypt_server_ble_key(encrypted_ble_key: str, native_key_secret: str = settings.NATIVE_KEY_SECRET) -> str:
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

    data = AES.new(_aes_key(key_text), AES.MODE_CBC, iv=iv).decrypt(encrypted)
    block_size = AES.block_size

    if not data or len(data) % block_size:
        raise RuntimeError("invalid padded data length")

    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        raise RuntimeError("invalid padding length")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise RuntimeError("invalid padding bytes")
    return data[:-pad_len].decode("utf-8").strip()


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

# region Nearby heybike scanning


@dataclass
class NearbyHeybike:
    """A very simple dataclass for capturing nearby heybike information from bleak"""
    mac: str
    name: str


async def iter_nearby_heybikes(
    scan_seconds: float = 20.0,
    *,
    name_prefix: str = "Heybike",
    service_uuid: str | None = settings.SERVICE_UUID,
) -> AsyncIterator[NearbyHeybike]:
    """
    Yield unique nearby HeyBike BLE advertisement records as they arrive.

    This deliberately performs discovery only.
        It does not connect, authenticate, fetch BLE keys, or send bike commands.

    Yields:
        NearbyHeybike: Just the bluetooth name and mac address of a nearby heybike.
    """

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[NearbyHeybike] = asyncio.Queue()
    seen: set[str] = set()
    wanted_prefix = name_prefix.lower()
    wanted_service = service_uuid.lower() if service_uuid else None

    def enqueue_once(record: NearbyHeybike):
        key = record.mac
        if key in seen:
            return
        seen.add(key)
        queue.put_nowait(record)

    def on_advertisement(device: BLEDevice, advertisement_data: AdvertisementData):
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

# endregion


def api_post(endpoint: str, fields: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    """A standard method for posting data to a Heybike API."""
    body = parse.urlencode(fields).encode("utf-8")
    req = request.Request(
        parse.urljoin(settings.API_BASE_URL, endpoint),
        data=body,
        headers={
            "systemtype": "android",
            "phoneInfo": settings.APP_VERSION,
            "phoneType": settings.PHONE_TYPE,
            "phoneSystems": settings.PHONE_SYSTEMS,
            "source": "1",
            "language": settings.LANGUAGE,
            "countryCode": settings.COUNTRY_CODE,
            "User-Agent": "okhttp/4.11.0",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8')

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API returned non-JSON from {endpoint}: {raw[:200]}") from exc

    status = str(parsed.get("status") or "")
    if status not in ("", "0", "1", "200"):
        message = parsed.get("message") or parsed.get("msg") or parsed
        raise HeybikeApiError(endpoint, status, message, parsed)
    return parsed


@dataclass
class Heybike:
    """A class for controlling Heybike-branded ebikes."""

    ble_key_cache_csv: ClassVar[str | Path | None] = os.environ.get("HEYBIKE_BLE_KEY_CACHE")
    _bike_models: ClassVar[list[BikeModelInfo]] = []
    _bike_colors: ClassVar[dict[str, list[BikeColorInfo]]] = defaultdict(list)

    mac: str
    name: str = ""
    model: BikeModelInfo | None = None
    color: BikeColorInfo | None = None
    email: str | None = None
    password: str | None = None
    token: str | None = None
    _imei: str | None = None
    _icc_id: str | None = None
    _ble_key: str | None = None
    _ble_token: bytes | None = None
    _hardware_version: int | None = None
    _iot_firmware_version: int | None = None
    _protocol_version: int | None = None

    # region Catalog methods

    @classmethod
    def bike_models(
        cls,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
        *,
        include_colors: bool = True,
        timeout: float = 30.0,
    ) -> list[BikeModelInfo]:
        """
        Return cached model metadata from `getAllBikeType`.

        When `include_colors` is true, each model is returned with its cached `getAllBikeColorType` results attached.

        Returns:
            list: The bike model info from the catalog.
        """

        token = cls._api_token_from_credentials(email=email, password=password, token=token)
        if cls._bike_models:
            models = cls._bike_models
        else:
            data = api_post("appHeyApi/getAllBikeType", {"token": token}, timeout=timeout)
            models = [BikeModelInfo.from_api(item)
                      for item in data.get("bikeTypes", [])
                      if isinstance(item, dict)]
            cls._bike_models = models

        if not include_colors:
            return models

        return [
            replace(model, colors=cls.bike_colors(token=token, de_type=model.id, timeout=timeout))
            if model.id is not None
            else model
            for model in models
        ]

    @classmethod
    def bike_colors(
        cls,
        de_type: int | str,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> list[BikeColorInfo]:
        """Return cached color metadata from `getAllBikeColorType`."""

        token = cls._api_token_from_credentials(email=email, password=password, token=token)
        de_type = str(de_type)
        if de_type in cls._bike_colors:
            return cls._bike_colors[de_type]

        data = api_post("appHeyApi/getAllBikeColorType", {"token": token, "deType": de_type}, timeout=timeout)
        colors = [BikeColorInfo.from_api(item)
                  for item in data.get("bikeTypes", [])
                  if isinstance(item, dict)]
        cls._bike_colors[de_type] = colors
        return colors

    # endregion

    @classmethod
    def account_bikes(
        cls,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ):
        """Fetch all of the bikes associated with a particular account from the APIs."""
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
            bike_type = item.get("bikeType", {})
            bike_color = item.get("bikeColor", {})
            name = str(item.get("nickName") or bike_type.get("typeName") or normalize_mac(mac))

            # TODO: Is nearby? Get list of all nearby_bikes below (owned=False) and cross reference.

            decrypted_ble_key = decrypt_server_ble_key(ble_key)
            imei = str(item.get("deIMEI") or "")
            _write_ble_key_cache(
                cls.ble_key_cache_csv,
                mac=mac,
                ble_key=decrypted_ble_key,
                imei=imei,
                name=name,
            )
            yield Heybike(
                name=name,
                mac=normalize_mac(mac),
                email=email,
                password=password,
                token=token,
                _ble_key=decrypted_ble_key,
                _imei=imei,
                model=BikeModelInfo.from_api(bike_type) if bike_type else None,
                color=BikeColorInfo.from_api(bike_color) if bike_color else None,
            )

    @classmethod
    async def nearby_bikes(
        cls,
        scan_seconds: float = 20,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
        *,
        owned: bool = False,
    ) -> AsyncIterator[Heybike]:
        """Fetch all of the nearby bikes using bleak."""
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

    def __eq__(self, other):  # ruff: ignore[missing-type-function-argument]
        return isinstance(other, Heybike) and self.mac == other.mac

    # region API login/token access
    @staticmethod
    @cache
    def _login(email: str, password: str) -> str:
        data = api_post(
            "appHeyApi/login",
            {
                "userEmail": email,
                "phoneInfo": settings.APP_VERSION,
                "phoneType": settings.PHONE_TYPE,
                "phoneSystems": settings.PHONE_SYSTEMS,
                "userPass": password,
            },
        )
        token = str(data.get("token") or "")
        if not token:
            raise RuntimeError(f"login succeeded but no token was returned: {data}")
        return token

    @classmethod
    def _api_token_from_credentials(
        cls,
        email: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ) -> str:
        if token is not None:
            return token
        if email is None or password is None:
            raise ValueError("Account information is needed to use the HeyBike API.")
        return cls._login(email, password)

    def _api_token(self) -> str:
        token = self._api_token_from_credentials(email=self.email, password=self.password, token=self.token)
        if token != self.token:
            self.token = token
        return self.token

    # endregion

    # region BLE communication
    # region BLE encryption

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

        Raises:
            ValueError: If no authentication methods have been provided.
            RuntimeError: If the API returns unexpected results.
        """
        if self._ble_key:
            return  # Bike info already gotten

        if email is not None:
            self.email = email
        if password is not None:
            self.password = password
        if token is not None:
            self.token = token

        cached_info = _read_ble_key_cache(self.ble_key_cache_csv, self.mac)
        if cached_info is not None:
            self._ble_key = cached_info.ble_key
            if not self._imei and cached_info.imei:
                self._imei = cached_info.imei
            if not self.name and cached_info.name:
                self.name = cached_info.name
            return

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
            raise RuntimeError(f"getBikeByBleMac returned no bike: {data}")  # ruff: ignore[type-check-without-type-error]
        ble_key = str(bike.get("bleKey") or bike.get("tBleKey") or "")
        if not ble_key:
            raise RuntimeError(f"getBikeByBleMac returned no bleKey: {data}")

        self._ble_key = decrypt_server_ble_key(ble_key)
        found_imei = str(bike.get("deIMEI") or bike.get("imei") or "")
        if not self._imei:
            self._imei = found_imei
        elif found_imei and self._imei != found_imei:
            logger.warning("The fetched IMEI doesn't match the stored value from the bike! %s vs %s",
                           self._imei, found_imei)
        found_name = str(bike.get("bleName") or bike.get("tBleName") or "")
        if found_name:
            self.name = found_name
        bike_type = bike.get("bikeType")
        bike_color = bike.get("bikeColor")
        if bike_type and self.model is None:
            self.model = BikeModelInfo.from_api(bike_type)
        if bike_color and self.color is None:
            self.color = BikeColorInfo.from_api(bike_color)
        _write_ble_key_cache(
            self.ble_key_cache_csv,
            mac=self.mac,
            ble_key=self._ble_key,
            imei=self._imei or "",
            name=self.name,
        )

    @property
    def ble_key(self) -> str:
        """The bike's BLE key"""
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
        force: bool = False,
    ) -> bytes:
        if self._ble_token and not force:
            return self._ble_token

        token_fetch_encrypted = _encrypt_ble_frame(TOKEN_PREFIX + bytes(13), ble_key)
        await client.write_gatt_char(write_uuid, token_fetch_encrypted, response=write_with_response)
        self._ble_token = await asyncio.wait_for(token_future, timeout=timeout)
        return self._ble_token

    # endregion

    async def send_ble_command(
        self,
        opcode: OpCode,
        payload: bytes = b"",
        *,
        timeout: float = 10.0,
        connect_timeout: float = 20.0,
        write_uuid: str = settings.WRITE_UUID,
        notify_uuid: str = settings.NOTIFY_UUID,
        write_with_response: bool = True,
    ) -> bytes:
        """Send one encrypted BLE command and return the matching clear response frame."""

        loop = asyncio.get_running_loop()
        token_future: asyncio.Future[bytes] = loop.create_future()
        response_future: asyncio.Future[bytes] = loop.create_future()

        def on_notify(_, data: bytearray):
            clear = decrypt_ble_notification(bytes(data), self.ble_key)
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
                    ble_key=self.ble_key,
                    write_uuid=write_uuid,
                    timeout=timeout,
                    write_with_response=write_with_response,
                )

                frame = _build_ble_command_frame(opcode.value, payload, token)
                encrypted = _encrypt_ble_frame(frame, self.ble_key)
                await client.write_gatt_char(write_uuid, encrypted, response=write_with_response)
                return await asyncio.wait_for(response_future, timeout=timeout)
            finally:
                await client.stop_notify(notify_uuid)

    # endregion

    async def get_base_info(self) -> BaseInfo:
        """Run the get_base_info command which fetches various info about the bike and fills in cache data"""
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

    # region Firmware updates

    async def check_for_updates(
        self,
        *,
        hardware_version: int | None = None,
        iot_firmware_version: int | None = None,
    ) -> FirmwareUpdate | None:
        """Return available IOT firmware metadata, or ``None`` when the bike is current."""

        if (hardware_version is None) != (iot_firmware_version is None):
            raise ValueError("hardware_version and iot_firmware_version must be supplied together")

        token = self._api_token()
        self._fill_bike_info(token=token)
        imei = self._imei or await self.get_imei()
        if not imei:
            raise RuntimeError("bike IMEI is unavailable; cannot query firmware updates")

        if hardware_version is None or iot_firmware_version is None:
            base_info = await self.get_base_info()
            hardware_version = base_info.hardware_version
            iot_firmware_version = base_info.iot_firmware_version

        data = api_post(
            "appHeyApi/getBikeIMEIUpload",
            {
                "token": token,
                "deIMEI": imei,
                "hardVersion": str(hardware_version),
                "oldVersion": str(iot_firmware_version),
            },
        )
        iot_info = data.get("iotInfo") or {}
        if not isinstance(iot_info, dict):
            iot_info = {}

        ota_url = str(iot_info.get("otaUrl") or "")
        if not ota_url:
            return None

        mode, filename = firmware_mode_and_name(imei, iot_info)
        return FirmwareUpdate(
            current_hardware_version=hardware_version,
            current_iot_firmware_version=iot_firmware_version,
            hardware_version=int(iot_info.get("otaHardVersion", hardware_version)),
            iot_firmware_version=int(iot_info.get("otaVersion", iot_firmware_version)),
            mode=mode,
            filename=filename,
            ota_url=ota_url,
            ftp_url=str(iot_info.get("ftpUrl") or ""),
            metadata=data,
        )

    async def update(
        self,
        firmware: bytes | bytearray | memoryview | str | Path | None = None,
        *,
        update_info: FirmwareUpdate | None = None,
        transfer_name: str | None = None,
        progress_callback: Callable[[int], object] | None = None,
        download_timeout: float = 120.0,
        timeout: float = 10.0,
        ota_response_timeout: float = 60.0,
        connect_timeout: float = 20.0,
        mtu: int = 150,
        second_start_delay: float = 2.0,
        settle_seconds: float = 5.0,
        write_uuid: str = settings.WRITE_UUID,
        notify_uuid: str = settings.NOTIFY_UUID,
        write_with_response: bool = True,
        wait_for_block_crc_after_header_ack: bool = False,
        force_ymode: bool = False,
    ) -> FirmwareUpdate | None:
        """Download an offered firmware update and apply it, or flash a supplied image."""

        if firmware is None:
            update_info = update_info or await self.check_for_updates()
            if update_info is None:
                return None
            if update_info.mode != "YMODE" and not force_ymode:
                raise FirmwareUpdateError(f"firmware mode {update_info.mode!r} is not supported")
            firmware_data = self._download_firmware(update_info.ota_url, timeout=download_timeout)
            default_transfer_name = update_info.filename
        else:
            firmware_data, default_transfer_name = self._read_firmware(firmware)
            if update_info is not None and update_info.mode != "YMODE" and not force_ymode:
                raise FirmwareUpdateError(f"firmware mode {update_info.mode!r} is not supported")
            if update_info is None and not force_ymode:
                imei = self._imei or await self.get_imei()
                if imei.startswith("88"):
                    raise FirmwareUpdateError(
                        "IMEI prefix 88 uses LIANZHAO OTA in the app; pass force_ymode=True only if this bike "
                        "actually uses YMODE",
                    )

        await self._send_ymodem_update(
            firmware_data,
            transfer_name or default_transfer_name or "firmware.bin",
            progress_callback=progress_callback,
            timeout=timeout,
            ota_response_timeout=ota_response_timeout,
            connect_timeout=connect_timeout,
            mtu=mtu,
            second_start_delay=second_start_delay,
            settle_seconds=settle_seconds,
            write_uuid=write_uuid,
            notify_uuid=notify_uuid,
            write_with_response=write_with_response,
            wait_for_block_crc_after_header_ack=wait_for_block_crc_after_header_ack,
        )
        return update_info

    @staticmethod
    def _read_firmware(firmware: bytes | bytearray | memoryview | str | Path) -> tuple[bytes, str | None]:
        if isinstance(firmware, (bytes, bytearray, memoryview)):
            return bytes(firmware), None

        path = Path(firmware)
        return path.read_bytes(), path.name

    @staticmethod
    def _download_firmware(url: str, *, timeout: float) -> bytes:
        req = request.Request(url, headers={"User-Agent": "okhttp/4.11.0"})
        with request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if not data:
            raise FirmwareUpdateError("downloaded firmware image is empty")
        return data

    @staticmethod
    async def _request_mtu(client: BleakClient, mtu: int):
        request_mtu = getattr(client, "request_mtu", None)
        if mtu <= 0 or not callable(request_mtu):
            return
        try:
            result = request_mtu(mtu)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # ruff: ignore[blind-except]
            logger.warning("BLE MTU request failed: %s", exc)

    async def _send_ymodem_update(
        self,
        firmware: bytes,
        transfer_name: str,
        *,
        progress_callback: Callable[[int], object] | None,
        timeout: float,
        ota_response_timeout: float,
        connect_timeout: float,
        mtu: int,
        second_start_delay: float,
        settle_seconds: float,
        write_uuid: str,
        notify_uuid: str,
        write_with_response: bool,
        wait_for_block_crc_after_header_ack: bool,
    ):
        engine = YModem128(
            firmware,
            transfer_name,
            send_first_block_after_header_ack=not wait_for_block_crc_after_header_ack,
        )
        ble_key = self.ble_key
        loop = asyncio.get_running_loop()
        token_future: asyncio.Future[bytes] = loop.create_future()
        ota_ready_future: asyncio.Future[bytes] = loop.create_future()
        start_ack_queue: asyncio.Queue[bytes] = asyncio.Queue()
        raw_ota_queue: asyncio.Queue[bytes] = asyncio.Queue()
        ota_enabled = False

        def on_notify(_sender: object, data: bytearray):
            raw = bytes(data)
            clear = decrypt_ble_notification(raw, ble_key)
            if clear is not None:
                if clear[:3] == TOKEN_PREFIX and not token_future.done():
                    loop.call_soon_threadsafe(_set_future_result, token_future, clear[4:8])
                    return
                if clear[0:2] == COMMAND_PREFIX and clear[2] == OpCode.OTA_START.value:
                    loop.call_soon_threadsafe(start_ack_queue.put_nowait, clear)
                    return
                if len(raw) == AES.block_size + 2 and raw[0] == NOTIFICATION_START and raw[-1] == NOTIFICATION_END:
                    return

            if ota_enabled and any(byte in YMODEM_CONTROL_BYTES for byte in raw):
                loop.call_soon_threadsafe(_set_future_result, ota_ready_future, raw)
                loop.call_soon_threadsafe(raw_ota_queue.put_nowait, raw)

        async def wait_for_start_confirmation(label: str):
            if ota_ready_future.done():
                logger.debug("OTA start raw readiness: %s", ota_ready_future.result().hex(" "))
                return

            ack_task = asyncio.create_task(start_ack_queue.get())
            try:
                done, _pending = await asyncio.wait(
                    {ack_task, ota_ready_future},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise FirmwareUpdateError(
                        f"timed out waiting for {label}: no encrypted OTA ACK or raw YMODEM control byte",
                    )
                if ack_task in done:
                    logger.debug("OTA start response: %s", ack_task.result().hex(" "))
                else:
                    logger.debug("OTA start raw readiness: %s", ota_ready_future.result().hex(" "))
            finally:
                if not ack_task.done():
                    ack_task.cancel()

        async def write_ota_start(client: BleakClient, token: bytes, payload: bytes):
            frame = _build_ble_command_frame(OpCode.OTA_START.value, payload, token)
            await client.write_gatt_char(write_uuid, _encrypt_ble_frame(frame, ble_key), response=write_with_response)

        async with BleakClient(self.mac, timeout=connect_timeout) as client:
            if not client.is_connected:
                raise FirmwareUpdateError(f"BLE connection failed for {self.mac}")

            await self._request_mtu(client, mtu)
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
                    force=True,
                )

                ota_enabled = True
                await write_ota_start(client, token, b"")
                await wait_for_start_confirmation("first OTA start command")

                if not ota_ready_future.done():
                    await asyncio.sleep(second_start_delay)
                    if not ota_ready_future.done():
                        await write_ota_start(client, token, b"\x01")
                        await wait_for_start_confirmation("second OTA start command")

                last_progress = -1
                while not engine.done:
                    try:
                        raw = await asyncio.wait_for(raw_ota_queue.get(), timeout=ota_response_timeout)
                    except asyncio.TimeoutError as exc:
                        raise FirmwareUpdateError(
                            f"timed out waiting for YMODEM receiver byte after {ota_response_timeout:.1f}s",
                        ) from exc

                    for byte in raw:
                        done, packet, event = engine.handle_byte(byte)
                        logger.debug("YMODEM recv 0x%02X: %s", byte, event)
                        if packet is not None:
                            await client.write_gatt_char(write_uuid, packet, response=write_with_response)
                            if progress_callback is not None and engine.progress != last_progress:
                                last_progress = engine.progress
                                progress_callback(engine.progress)
                        if done:
                            break

                await asyncio.sleep(settle_seconds)
            finally:
                await client.stop_notify(notify_uuid)

    # endregion

    async def get_hardware_version(self) -> int:
        """Get the bike's hardware version"""
        if self._hardware_version is None:
            await self.get_base_info()

        return self._hardware_version

    async def get_iot_firmware_version(self) -> int:
        """Get the bike's IOT firmware version"""
        if self._iot_firmware_version is None:
            await self.get_base_info()

        return self._iot_firmware_version

    async def get_protocol_version(self) -> int | None:
        """Get the bike's protocol version"""
        if self._protocol_version is None:
            await self.get_base_info()

        return self._protocol_version

    async def get_battery_percent(self) -> int:
        """Get the bike's battery percentage"""
        return (await self.get_base_info()).battery_percent

    async def get_auto_lock(self) -> bool:
        """Get the bike's auto-lock status"""
        return (await self.get_base_info()).auto_lock_enabled

    async def get_auto_lock_info(self) -> AutoLockInfo:
        """Get the bike's auto-lock status and timeout"""
        response = await self.send_ble_command(OpCode.AUTO_LOCK)
        if len(response) <= 6:
            raise RuntimeError("auto-lock response did not include all expected fields")

        enabled = response[4] == 1
        time = _u16be(response, 5)
        return AutoLockInfo(enabled=enabled, time=time if enabled else 0)

    async def set_auto_lock(self, enabled: bool, time: int = 0):  # ruff: ignore[boolean-type-hint-positional-argument]
        """Set the bike's auto-lock status and timeout"""
        enabled = bool(enabled)
        payload = (b"\x01" if enabled else b"\x00") + _u16be_payload(time if enabled else 0, "auto-lock time")
        await self.send_ble_command(OpCode.AUTO_LOCK, payload)

    async def get_mileage(self) -> float:
        """Get the bike's current mileage in miles."""
        response = await self.send_ble_command(OpCode.MILEAGE)
        if len(response) < 8:
            raise RuntimeError("mileage response did not include all expected fields")
        return int.from_bytes(response[4:8], "big") * MPH_FACTOR

    async def get_imei(self) -> str:
        """Get the bike's IMEI"""
        first = await self.send_ble_command(OpCode.IMEI_FIRST)
        second = await self.send_ble_command(OpCode.IMEI_SECOND)
        if len(first) < 12 or len(second) < 4:
            raise RuntimeError("IMEI response did not include all expected fields")

        second_len = min(second[3], 8)
        found_imei = _ascii_clean(first[4:12] + second[4 : 4 + second_len])
        if not self._imei:
            self._imei = found_imei
        elif self._imei != found_imei:
            logger.warning("The fetched IMEI doesn't match the stored value from the APIs! %s vs %s",
                           self._imei, found_imei)
        return found_imei

    async def get_icc_id(self) -> str:
        """Get the bike's ICC ID"""
        if self._icc_id:
            return self._icc_id

        if not (self._imei or await self.get_imei()).startswith("86"):
            raise NotImplementedError("This bike does not support this endpoint.")

        first = await self.send_ble_command(OpCode.ICCID_FIRST)
        second = await self.send_ble_command(OpCode.ICCID_SECOND)
        final = await self.send_ble_command(OpCode.ICCID_FINAL)
        if len(first) < 12 or len(second) < 12 or len(final) < 9:
            raise RuntimeError("ICCID response did not include all expected fields")

        self._icc_id = _ascii_clean(first[4:12] + second[4:12] + bytes(byte for byte in final[4:9] if byte))
        return self._icc_id

    @cache
    async def bike_info_by_imei(
        self,
        *,
        timeout: float = 30.0,
    ) -> BikeIdentityInfo | None:
        """
        Return metadata from `getBikeByIMEI`.

        Returns:
            BikeIdentityInfo: Details about the bike if the call was successful.
            None: In the event the server returns status `209` for already-owned bikes that are not accessible
                to the current account.

        Raises:
            HeybikeApiError: If there was an unexpected error from the API.
        """

        token = self._api_token()
        imei = self._imei or await self.get_imei()
        try:
            data = api_post("appHeyApi/getBikeByIMEI",
                            {"token": token, "deIMEI": imei},
                            timeout=timeout)
        except HeybikeApiError as exc:
            if exc.status == 209:
                return None
            raise

        return BikeIdentityInfo.from_api(imei, data)

    async def reset_to_default(self):
        """Reset the bike's personalization settings to defaults"""
        await self.send_ble_command(OpCode.RESET_TO_DEFAULT)

    async def reset_trip_distance(self):
        """Reset the bike's trip distance"""
        await self.send_ble_command(OpCode.RESET_TRIP_DISTANCE)

    async def sync_controller_time(self, when: dt.datetime | None = None):
        """Sync the bike controller time from the local clock"""
        when = when or dt.datetime.now()
        payload = bytes([
            when.second,
            when.minute,
            when.hour,
            when.day,
            when.month,
            when.isoweekday() % 7,
            when.year & 0xFF,
            (when.year >> 8) & 0xFF,
        ])
        await self.send_ble_command(OpCode.SYNC_TIME, payload)

    async def get_signal_gps(self) -> SignalGpsInfo:
        """Get the bike's cellular signal and GPS signal levels"""
        response = await self.send_ble_command(OpCode.SIGNAL_GPS)
        if len(response) <= 5:
            raise RuntimeError("signal/GPS response did not include all expected fields")
        return SignalGpsInfo(signal_intensity=response[4], gps_signal=response[5])

    async def get_anti_theft(self) -> AntiTheftInfo:
        """Get the bike's anti-theft/fence status"""
        response = await self.send_ble_command(OpCode.ANTI_THEFT)
        if len(response) <= 6:
            raise RuntimeError("anti-theft response did not include all expected fields")
        return AntiTheftInfo(enabled=response[4] == 1, distance=_u16be(response, 5))

    async def set_anti_theft(
        self,
        enabled: bool,  # ruff: ignore[boolean-type-hint-positional-argument]
        distance: int = 0,
    ):
        """Set the bike's anti-theft/fence status"""
        enabled = bool(enabled)
        payload = (b"\x01" if enabled else b"\x00") + _u16be_payload(distance if enabled else 0, "anti-theft distance")
        await self.send_ble_command(OpCode.ANTI_THEFT, payload)

    async def get_max_speed(self) -> int:
        """Get the bike's max speed"""
        imei = self._imei or await self.get_imei()
        response = await self.send_ble_command(OpCode.MAX_SPEED)
        raw_value = _u16be(response)
        if imei.startswith("86"):
            return int(raw_value / MPH_FACTOR + 0.5)
        return raw_value

    async def set_max_speed(self, value: int):
        """Set the bike's max speed"""
        if value < 0:
            raise ValueError("max speed must be non-negative")

        raw_value = value
        if (self._imei or await self.get_imei()).startswith("86"):
            raw_value = int(value * MPH_FACTOR + 0.5)

        response = await self.send_ble_command(OpCode.MAX_SPEED, _u16be_payload(raw_value, "max speed"))
        reported = _u16be(response)
        if reported != raw_value:
            raise RuntimeError(f"bike reported max-speed raw value {reported}, expected {raw_value}")

    async def get_speed_unit(self) -> int:
        """Get the bike's speed unit"""
        response = await self.send_ble_command(OpCode.SPEED_UNIT)
        return _u16be(response)

    async def set_speed_unit(self, value: int):
        """Set the bike's speed unit, where 0 is km and 1 is mile"""
        if value not in (0, 1):
            raise ValueError("speed unit must be 0 for km or 1 for mile")
        await self.send_ble_command(OpCode.SPEED_UNIT, _u16be_payload(value, "speed unit"))

    async def get_voltage_level(self) -> int:
        """Get the bike's voltage level"""
        response = await self.send_ble_command(OpCode.VOLTAGE)
        return _u16be(response)

    async def get_drive_gear(self) -> int:
        """Get the bike's drive gear"""
        response = await self.send_ble_command(OpCode.DRIVE_GEAR)
        return _u16be(response)

    async def set_drive_gear(self, value: int):
        """Set the bike's drive gear"""
        await self.send_ble_command(OpCode.DRIVE_GEAR, _u16be_payload(value, "drive gear"))

    async def get_start_gear(self) -> int:
        """Get the bike's start gear"""
        response = await self.send_ble_command(OpCode.START_GEAR)
        return _u16be(response)

    async def set_start_gear(self, value: int):
        """Set the bike's start gear"""
        await self.send_ble_command(OpCode.START_GEAR, _u16be_payload(value, "start gear"))

    async def get_backlight_brightness(self) -> int:
        """Get the bike's backlight brightness"""
        response = await self.send_ble_command(OpCode.BACKLIGHT_BRIGHTNESS)
        value = _u16be(response)
        return value - 4 if value > 4 else value

    async def set_backlight_brightness(self, value: int):
        """Set the bike's backlight brightness"""
        if value < 0:
            raise ValueError("backlight brightness must be non-negative")
        raw_value = 4 if value == 0 else value + 4 if value < 4 else min(value, 6)
        await self.send_ble_command(OpCode.BACKLIGHT_BRIGHTNESS, _u16be_payload(raw_value, "backlight brightness"))

    async def get_handle_pwm(self) -> tuple[int, int, int, int, int]:
        """Get the bike's handle PWM values"""
        response = await self.send_ble_command(OpCode.HANDLE_PWM)
        if len(response) <= 8:
            raise RuntimeError("handle PWM response did not include all expected fields")
        return response[4], response[5], response[6], response[7], response[8]

    async def set_handle_pwm(self, values: bytes | bytearray | tuple[int, ...] | list[int]):
        """Set the bike's handle PWM values"""
        payload = bytes(values)
        if len(payload) != 5:
            raise ValueError("handle PWM must include exactly five values")
        await self.send_ble_command(OpCode.HANDLE_PWM, payload)

    async def get_handle_gear(self) -> int:
        """Get the bike's handle gear"""
        response = await self.send_ble_command(OpCode.HANDLE_GEAR)
        return _u16be(response)

    async def set_handle_gear(self, value: int):
        """Set the bike's handle gear"""
        await self.send_ble_command(OpCode.HANDLE_GEAR, _u16be_payload(value, "handle gear"))

    async def get_speed_limiter_type(self) -> int:
        """Get the bike's speed limiter type"""
        response = await self.send_ble_command(OpCode.SPEED_LIMITER_TYPE)
        return _u16be(response)

    async def set_speed_limiter_type(self, value: int):
        """Set the bike's speed limiter type, where 0 is both, 1 is PAS, and 2 is throttle"""
        if value not in (0, 1, 2):
            raise ValueError("speed limiter type must be 0 for both, 1 for PAS, or 2 for throttle")
        await self.send_ble_command(OpCode.SPEED_LIMITER_TYPE, _u16be_payload(value, "speed limiter type"))

    async def get_ride_feel(self) -> int:
        """Get the bike's ride feel"""
        response = await self.send_ble_command(OpCode.RIDE_FEEL)
        return _u16be(response)

    async def set_ride_feel(self, value: int):
        """Set the bike's ride feel"""
        await self.send_ble_command(OpCode.RIDE_FEEL, _u16be_payload(value, "ride feel"))

    async def get_preset_mode(self) -> int:
        """Get the bike's preset mode"""
        response = await self.send_ble_command(OpCode.PRESET_MODE)
        if len(response) <= 4:
            raise RuntimeError("preset mode response did not include all expected fields")
        return response[4]

    async def set_preset_mode(self, value: int):
        """Set the bike's preset mode"""
        if value not in (1, 2, 3):
            raise ValueError("preset mode must be 1, 2, or 3")
        await self.send_ble_command(OpCode.PRESET_MODE, bytes([value]))

    async def get_throttle_sensitivity(self) -> int:
        """Get the bike's throttle sensitivity"""
        response = await self.send_ble_command(OpCode.THROTTLE_SENSITIVITY)
        return _u16be(response)

    async def set_throttle_sensitivity(self, value: int):
        """Set the bike's throttle sensitivity"""
        await self.send_ble_command(OpCode.THROTTLE_SENSITIVITY, _u16be_payload(value, "throttle sensitivity"))

    async def get_headlight(self) -> bool:
        """Get the bike's headlight state"""
        response = await self.send_ble_command(OpCode.HEADLIGHT)
        return _u16be(response) == 1

    async def set_headlight(self, value: bool):  # ruff: ignore[boolean-type-hint-positional-argument]
        """Set the bike's headlight state"""
        value = bool(value)
        await self.send_ble_command(OpCode.HEADLIGHT, _u16be_payload(1 if value else 0, "headlight"))

    async def get_power(self) -> bool:
        """Get the bike power state"""
        return (await self.get_base_info()).power_on

    async def set_power(self, value: bool):  # ruff: ignore[boolean-type-hint-positional-argument]
        """Set the bike power state."""

        value = bool(value)
        response = await self.send_ble_command(OpCode.POWER, b"\x01" if value else b"\x00")
        if len(response) <= 4:
            raise RuntimeError("power response did not include the status byte")

        reported = bool(response[4])
        if reported != value:
            raise RuntimeError(f"bike reported power={reported}, expected power={value}")

    async def toggle_power(self) -> bool:
        """Toggle the power state manually"""
        new_power = not await self.get_power()
        await self.set_power(new_power)
        return new_power
