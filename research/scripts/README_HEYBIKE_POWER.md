# HeyBike BLE Power Script

Script: `tools/heybike_power.py`

This is a minimal Python recreation of the app's BLE power flow. It does not implement the whole app or OTA update flow.

## Dependencies

```powershell
py -3 -m pip install bleak pycryptodome
```

## What You Need

The normal path is your HeyBike account login. The script logs in, fetches your account bikes, decrypts the server `bleKey`, scans for nearby `Heybike*` BLE devices, and connects to the matching bike.

```powershell
py -3 tools\heybike_power.py on --email "you@example.com"
py -3 tools\heybike_power.py off --email "you@example.com"
```

If you omit `--password`, the script prompts securely.

When the account has multiple bikes, the script lists them and asks for an index. You can avoid the prompt:

```powershell
py -3 tools\heybike_power.py on --email "you@example.com" --bike-index 0
py -3 tools\heybike_power.py on --email "you@example.com" --bike-name "Ranger"
py -3 tools\heybike_power.py on --email "you@example.com" --bike-mac "AA:BB:CC:DD:EE:FF"
```

## Manual Key Modes

You can still bypass login if you already have one usable BLE command key.

Best case:

```text
--key <transformed_key>
```

This is the plaintext/transformed key after the app's `vl.a.b(bleKey)` step. The app then uses this string's UTF-8 bytes directly as the AES key for BLE command encryption.

If you only have the server/account `bleKey`, use:

```text
--encrypted-key <server_bleKey>
```

The native constants are now recovered from `libbikeKey.so`:

```text
getCryptAlgorithm()       AES
getCryptTransformation()  AES/ECB/NoPadding
getCryptTransformation2() AES/CBC/PKCS5Padding
getKeySecret()            a70948d8a93b9dab:0102930405060708
```

## Run

Login and auto-select account bike when possible:

```powershell
py -3 tools\heybike_power.py on --email "you@example.com" --verbose
```

Scan by BLE name with a manual key:

```powershell
py -3 tools\heybike_power.py on --name HeyBike --key "<transformed_key>" --verbose
```

Use a known BLE address:

```powershell
py -3 tools\heybike_power.py off --address "AA:BB:CC:DD:EE:FF" --key "<transformed_key>" --verbose
```

Use an encrypted server `bleKey` after recovering native constants:

```powershell
py -3 tools\heybike_power.py on --address "AA:BB:CC:DD:EE:FF" --encrypted-key "<server_bleKey>" --verbose
```

Dry-run frame generation:

```powershell
py -3 tools\heybike_power.py on --key "<transformed_key>" --dry-run --token "00 00 00 00"
```

## Discovery Helper

`heybike_power.py` exposes an async generator for discovery-only workflows:

```python
from heybike_power import compact_mac, iter_nearby_heybike_macs


async def visible_owned_bike_macs(account_bikes):
    owned = {compact_mac(bike.mac) for bike in account_bikes}
    async for mac in iter_nearby_heybike_macs(scan_seconds=20):
        if compact_mac(mac) in owned:
            yield mac
```

The generator only scans advertisements and yields normalized MAC/address values.
It does not connect, request BLE keys, or send commands.

## Protocol Notes

Normal BLE UUIDs:

- Service: `86531001-43e6-47b7-9cb0-5fc21d4ae340`
- Write: `86531002-43e6-47b7-9cb0-5fc21d4ae340`
- Notify: `86531003-43e6-47b7-9cb0-5fc21d4ae340`

Token fetch clear frame:

```text
16 5A 01 00 00 00 00 00 00 00 00 00 00 00 00 00
```

Power clear frame before encryption:

```text
61 62 31 01 <01-or-00> 00 00 00 00 00 00 00 <4-byte-token>
```

The command encryption mode is inferred as `AES/ECB/NoPadding` because normal app frames are exactly 16 bytes and notification payloads contain exactly 16 encrypted bytes wrapped by `{` and `}`.

## Current Limits

The script has not been tested against a live bike in this workspace. Run it with the bike stationary and expect one or two BLE write-mode tweaks may be needed depending on platform.

## Server ACL Check

Script: `tools/heybike_acl_check.py`

This tests one owned bike against one second account. It does not scan nearby bikes, generate MAC ranges, or query arbitrary devices.

Create or use a second HeyBike account that does not have your bike bound, then run:

```powershell
py -3 tools\heybike_acl_check.py --owner-email "owner@example.com" --test-email "test@example.com"
```

If your owner account has multiple bikes:

```powershell
py -3 tools\heybike_acl_check.py --owner-email "owner@example.com" --test-email "test@example.com" --bike-index 0
```

Expected secure result:

```text
PASS: endpoint returned no BLE key.
```

Bad result:

```text
FAIL: unassociated test account received a BLE key for the owned bike.
```

## Firmware Acquisition

Script: `tools/heybike_firmware_download.py`

The app does not expose a current-firmware readback path in the visible protocol. Its OTA flow reads the current version over BLE, asks the server for update metadata, then downloads `otaUrl` if the server returns one.

Download update firmware if available:

```powershell
py -3 tools\heybike_firmware_download.py --email "you@example.com"
```

Print OTA metadata too:

```powershell
py -3 tools\heybike_firmware_download.py --email "you@example.com" --print-json
```

Only query metadata and do not download:

```powershell
py -3 tools\heybike_firmware_download.py --email "you@example.com" --metadata-only --print-json
```

If BLE version reading fails but you know the current version:

```powershell
py -3 tools\heybike_firmware_download.py --email "you@example.com" --hard-version 1 --old-version 23
```

The script writes firmware files under `firmware\` by default and stores the API response next to the file as JSON.

## Firmware Update

Script: `tools/heybike_firmware_update.py`

This recreates the app's YMODE OTA path for non-`88` IMEIs. It sends opcode
`0x35`, waits, sends `0x35 01`, then transfers the file using the app's
128-byte YMODEM variant. Use only official firmware for your own bike.

```powershell
py -3 tools\heybike_firmware_update.py tools\firmware\upgradeFile_10.vmfw --email "you@example.com"
```

Non-interactive runs require an explicit confirmation flag:

```powershell
py -3 tools\heybike_firmware_update.py tools\firmware\upgradeFile_10.vmfw --email "you@example.com" --yes --non-interactive
```

The script does not implement the `LIANZHAO` OTA mode used by app-detected
`88...` IMEIs.

## Firmware Triage

Script: `tools/firmware_triage.py`

Quickly check whether a downloaded firmware file has obvious headers, strings, vector tables, or high entropy:

```powershell
py -3 tools\firmware_triage.py tools\firmware\upgradeFile_10.vmfw
```

The current `upgradeFile_10.vmfw` is very high entropy, so it is probably encrypted or compressed before the bike bootloader receives it.

## BLE Opcode Probe

Script: `tools/heybike_ble_probe.py`

Read the decrypted responses for known non-mutating commands:

```powershell
py -3 tools\heybike_ble_probe.py --email "you@example.com" --command base,imei,iccid
py -3 tools\heybike_ble_probe.py --email "you@example.com" --all-read
```

For the opcode table, see `PROTOCOL_OPCODE_MAP.md`.

## Model and Color Catalog Probe

Script: `tools/heybike_catalog_probe.py`

Inspect the account/model/color API data without BLE:

```powershell
py -3 tools\heybike_catalog_probe.py --email "you@example.com"
```

Catalog-only check, avoiding `getUserBikes`. This calls `getAllBikeType`, then
calls `getAllBikeColorType` for every returned bike type and prints the full
color fields:

```powershell
py -3 tools\heybike_catalog_probe.py --email "you@example.com" --skip-user-bikes
```

Query colors for a specific `deType`:

```powershell
py -3 tools\heybike_catalog_probe.py --email "you@example.com" --skip-user-bikes --color-de-type 8 --raw
```

Skip the per-type color expansion:

```powershell
py -3 tools\heybike_catalog_probe.py --email "you@example.com" --skip-type-colors
```

If you read an IMEI over BLE and want to test the non-`getUserBikes` metadata
path:

```powershell
py -3 tools\heybike_catalog_probe.py --email "you@example.com" --skip-user-bikes --skip-types --bike-imei "876404338843817" --raw
```

Raw/output JSON redacts tokens and BLE keys by default. Use `--no-redact` only
for local private inspection.

## Headlight Control

Script: `tools/heybike_headlight.py`

Read or change opcode `0x42`, matching the app's headlight command:

```powershell
py -3 tools\heybike_headlight.py read --email "you@example.com"
py -3 tools\heybike_headlight.py on --email "you@example.com"
py -3 tools\heybike_headlight.py off --email "you@example.com"
py -3 tools\heybike_headlight.py toggle --email "you@example.com"
```

Raw values are also supported:

```powershell
py -3 tools\heybike_headlight.py set --value 1 --email "you@example.com" --read-after-write
```

## Max Speed Control

Script: `tools/heybike_max_speed.py`

Read or change opcode `0xE1`, matching the app's max-speed command:

```powershell
py -3 tools\heybike_max_speed.py read --email "you@example.com"
py -3 tools\heybike_max_speed.py set --value 25 --email "you@example.com" --read-after-write
```

For IMEIs starting with `86`, the app converts between displayed km/h-style
values and raw BLE values. The script follows that behavior when the IMEI is
known from your account. To bypass conversion:

```powershell
py -3 tools\heybike_max_speed.py read --email "you@example.com" --raw
py -3 tools\heybike_max_speed.py set --value 16 --email "you@example.com" --raw
```

## Speed Unit Control

Script: `tools/heybike_speed_unit.py`

Read or change opcode `0xE5`, matching the app's speed-unit command:

```powershell
py -3 tools\heybike_speed_unit.py read --email "you@example.com"
py -3 tools\heybike_speed_unit.py km --email "you@example.com" --read-after-write
py -3 tools\heybike_speed_unit.py mile --email "you@example.com" --read-after-write
```

Raw values are also supported:

```powershell
py -3 tools\heybike_speed_unit.py set --value 0 --email "you@example.com"
py -3 tools\heybike_speed_unit.py set --value 1 --email "you@example.com"
```

## Ride Feel Control

Script: `tools/heybike_ride_feel.py`

Read or change opcode `0x44`, matching the app's ride-feel command:

```powershell
py -3 tools\heybike_ride_feel.py read --email "you@example.com"
py -3 tools\heybike_ride_feel.py set --value 1 --email "you@example.com" --read-after-write
```

Ride-feel labels are model-specific in the app, so this script keeps the values numeric.

## Speed Limiter Type Control

Script: `tools/heybike_speed_independent.py`

The app calls opcode `0xDC` `speedIndependent`, but the UI labels it as speed
limiter type:

```powershell
py -3 tools\heybike_speed_independent.py read --email "you@example.com"
py -3 tools\heybike_speed_independent.py both --email "you@example.com" --read-after-write
py -3 tools\heybike_speed_independent.py pas --email "you@example.com" --read-after-write
py -3 tools\heybike_speed_independent.py throttle --email "you@example.com" --read-after-write
```

Values are `0 = both`, `1 = PAS`, `2 = throttle`.
