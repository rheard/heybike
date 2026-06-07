import argparse
import asyncio
import base64
import binascii
import getpass
import json
import platform
import sys
from dataclasses import dataclass, field
from functools import cache
from typing import Any, AsyncIterator, Optional
from urllib import parse, request

try:
    from bleak import BleakClient, BleakScanner, BLEDevice, AdvertisementData
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
API_BASE_URL = "https://heyapi.heybike.com/"
APP_VERSION = "v4.6.0"
PHONE_TYPE = f"{platform.system()}/python:{platform.machine() or 'unknown'}"
PHONE_SYSTEMS = f"OS Version:{platform.platform()}"
LANGUAGE = "en"
COUNTRY_CODE = "US"


@dataclass
class NearbyHeybike:
    mac: str
    name: str


def compact_mac(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch in "0123456789ABCDEF")


def normalize_mac(value: str) -> str:
    compact = compact_mac(value)
    if len(compact) == 12:
        return ":".join(compact[i : i + 2] for i in range(0, 12, 2))
    return value.upper()


async def iter_nearby_heybikes(
    scan_seconds: float = 20.0,
    *,
    name_prefix: str = "Heybike",
    service_uuid: Optional[NearbyHeybike] = SERVICE_UUID,
) -> AsyncIterator[str]:
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

    @classmethod
    def account_bikes(cls, email=None, password=None, token=None, nearby=False):
        if (email is None or password is None) and token is None:
            raise ValueError("Account information is needed to get bikes associated with an account")

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
                _ble_key=ble_key,
                _imei=str(item.get("deIMEI") or ""),
            )

    @classmethod
    async def nearby_bikes(cls, scan_seconds=20, email=None, password=None, token=None, owned=False):
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

    def _fill_bike_info(self, email=None, password=None, token=None):
        if self._ble_key:
            return  # Bike info already gotten

        if (email is None or password is None) and token is None:
            # TODO: Critically this API does NOT authenticate that the associated bike belongs to this account
            raise ValueError("Account information is needed to get complete bike information.")

        data = api_post("appHeyApi/getBikeByBleMac", {"token": token, "deBle": normalize_mac(self.mac)})
        bike = data.get("bike")
        if not isinstance(bike, dict):
            raise RuntimeError(f"getBikeByBleMac returned no bike: {data}")
        ble_key = str(bike.get("bleKey") or bike.get("tBleKey") or "")
        if not ble_key:
            raise RuntimeError(f"getBikeByBleMac returned no bleKey: {data}")

        self._ble_key = ble_key
        self._imei = str(bike.get("deIMEI") or bike.get("imei") or "")
        self.name = str(bike.get("bleName") or bike.get("tBleName") or "")
