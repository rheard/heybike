# heybike

Python control and firmware-update helpers for Heybike e-bikes.

`heybike` wraps the Heybike account APIs and the bike's BLE protocol in a small async Python interface. It can find bikes from your account, scan for nearby Heybike BLE advertisements, read common bike state, change controller/display settings, and apply supported firmware updates.

This is an unofficial package. I have only been able to test it against the bikes I own, primarily a Heybike Cityrun 1.0, so expect some model-specific edges.

---

## Installation

```bash
python -m pip install heybike
```

Bluetooth access is provided by [`bleak`](https://github.com/hbldh/bleak), so your machine needs a working Bluetooth adapter and the usual OS-level BLE permissions.

---

## Quickstart

```python
import asyncio

from heybike import Heybike

EMAIL = "you@example.com"
PASSWORD = "your-heybike-password"


async def main():
    bike = next(Heybike.account_bikes(email=EMAIL, password=PASSWORD))

    info = await bike.get_base_info()
    print(bike.name, bike.mac)
    print(f"battery: {info.battery_percent}%")
    print(f"power: {info.power_on}")

    await bike.set_headlight(True)
    await bike.set_power(False)


asyncio.run(main())
```

Most bike operations are async because they connect to the bike over BLE.

---

## Finding bikes

Get the bikes already associated with your Heybike account:

```python
from heybike import Heybike

for bike in Heybike.account_bikes(email=EMAIL, password=PASSWORD):
    print(bike.name, bike.mac, bike.model.name if bike.model else "")
```

Scan for nearby Heybike BLE advertisements:

```python
import asyncio

from heybike import Heybike


async def main():
    async for bike in Heybike.nearby_bikes(
        scan_seconds=10,
        email=EMAIL,
        password=PASSWORD,
    ):
        print(bike.name, bike.mac)


asyncio.run(main())
```

The optional `HEYBIKE_BLE_KEY_CACHE` environment variable can point at a CSV file used to cache BLE keys:

```bash
set HEYBIKE_BLE_KEY_CACHE=%USERPROFILE%\.heybike_ble_keys.csv
```

---

## Bike state and controls

Common read methods:

- `get_base_info()` for battery, power, auto-lock, hardware, firmware, and protocol versions.
- `get_battery_percent()`, `get_power()`, `get_mileage()`, `get_imei()`, and `get_icc_id()`.
- `get_signal_gps()` for cellular/GPS signal levels.
- `get_auto_lock_info()` and `get_anti_theft()`.

Common write methods:

- `set_power(...)`, `toggle_power()`, and `set_headlight(...)`.
- `set_auto_lock(...)` and `set_anti_theft(...)`.
- `set_max_speed(...)`, `set_speed_unit(...)`, `set_drive_gear(...)`, and `set_start_gear(...)`.
- `set_backlight_brightness(...)`, `set_ride_feel(...)`, `set_preset_mode(...)`, and `set_throttle_sensitivity(...)`.
- `reset_trip_distance()`, `reset_to_default()`, and `sync_controller_time()`.

Example:

```python
async def configure(bike: Heybike):
    await bike.sync_controller_time()
    await bike.set_speed_unit(1)  # 0 = km, 1 = mile
    await bike.set_auto_lock(True, time=10)
    await bike.set_backlight_brightness(3)
```

---

## Firmware updates

`heybike` can ask the Heybike API whether an OTA update is available, download it, and transfer it over BLE using the YMODEM variant used by supported bikes.

```python
import asyncio

from heybike import Heybike


async def main():
    bike = next(Heybike.account_bikes(email=EMAIL, password=PASSWORD))

    update = await bike.check_for_updates()
    if update is None:
        print("Already current")
        return

    print(f"{update.current_version} -> {update.version}")
    await bike.update(
        update_info=update,
        progress_callback=lambda progress: print(f"{progress}%"),
    )


asyncio.run(main())
```

You can also pass a local firmware image to `update(...)`, but firmware updates are inherently risky. Make sure the image and OTA mode match your bike.

---

## Catalog metadata

The package exposes a small amount of model/color metadata from the Heybike APIs:

```python
from heybike import Heybike

models = Heybike.bike_models(email=EMAIL, password=PASSWORD)
for model in models:
    print(model.name, [color.name for color in model.colors])
```

The main public data classes are `BaseInfo`, `AutoLockInfo`, `AntiTheftInfo`, `SignalGpsInfo`, `BikeModelInfo`, `BikeColorInfo`, `BikeIdentityInfo`, and `FirmwareUpdate`.

---

## Notes

- Use this only with bikes you own or are authorized to work on.
- Heybike can change their app, APIs, firmware formats, or BLE behavior at any time.
- Firmware transfer support is currently centered on the YMODEM update flow.
- The research notes and protocol background live in [`research/`](research/), but normal package use should not require reading them.
