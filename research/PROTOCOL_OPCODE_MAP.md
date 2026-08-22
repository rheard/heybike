# HeyBike BLE Opcode Map

This is from the decompiled app, mostly `sources/com/yulai/heybike/bluetooth/w.java`
for command construction and `sources/com/yulai/heybike/bluetooth/v.java` for
response parsing.

Use this only against your own bike. Start with read commands. Several write
commands alter controller settings.

## Frame Shape

Normal cleartext command before encryption:

```text
61 62 <opcode> <payload_len> <payload...> <padding...> <token[4]>
```

The app then AES-encrypts the 16-byte frame and writes it to the BLE write
characteristic.

Notifications are wrapped as:

```text
7b <encrypted 16-byte frame> 7d
```

The live token is obtained with the special 16-byte command:

```text
16 5a 01 00 00 00 00 00 00 00 00 00 00 00 00 00
```

## Known Read Commands

| Opcode | App Symbol | Meaning | Response notes |
| --- | --- | --- | --- |
| `0x38` | `f31603j` | Base info | `[4]` error, `[5]` battery percent, `[6]` auto-lock, `[7]` power, `[8]` hardware version, `[9]` IoT firmware version, `[10]` protocol version |
| `0x39` | `f31604k` | Mileage | App parses bytes `4..7` with `vl.c.h()` |
| `0xa0` | `f31605l` | IMEI first part | Stores 8 bytes from offset `4` |
| `0xa1` | grouped part | IMEI second part | App concatenates with `0xa0` bytes as UTF-8 |
| `0x20` | `f31606m` | ICCID first part | Stores 8 bytes at offset `0` |
| `0x21` | grouped part | ICCID second part | Stores 8 bytes at offset `8` |
| `0x22` | grouped part | ICCID final part | Copies nonzero bytes from response bytes `4..8` |
| `0xd1` | `K` | Signal/GPS | `[4]` signal intensity, `[5]` GPS signal |
| `0xd7` | `L` | Anti-theft/fence status | `[4]` enabled, distance parsed at offset `5` |
| `0xd8` | `f31607n` | Auto-lock | `[4]` enabled, time parsed at offset `5` |
| `0xe1` | `E` | Max speed | Big-endian integer parsed at offset `4`; IMEI prefix `86` applies app conversion |
| `0xe5` | `f31611r` | Speed unit | Big-endian integer parsed at offset `4`; app labels `0` as `km`, `1` as `mile` |
| `0xe6` | `f31613t` | Voltage level | Integer parsed at offset `4` |
| `0xe7` | `f31614u` | Drive/bike gear | Integer parsed at offset `4` |
| `0xea` | `A` | Start gear | Integer parsed at offset `4` |
| `0xef` | `C` | Backlight brightness | Integer parsed at offset `4`; app subtracts 4 if value is above 4 |
| `0xda` | `G` | Handle PWM | Raw bytes `[4..8]` become comma-separated values |
| `0xdb` | `f31616w` | Handle gear | Integer parsed at offset `4` |
| `0xdc` | `f31609p` | Speed independent / speed limiter type | Big-endian integer parsed at offset `4`; app labels `0` both, `1` PAS, `2` throttle |
| `0x44` | `R` | Ride feel | Big-endian integer parsed at offset `4`; labels are model-specific |
| `0xdf` | `T` | Preset mode | Byte `[4]`; app accepts values `1..3` |
| `0x49` | `I` | Throttle sensitivity | Integer parsed at offset `4` |
| `0x42` | `P` | Headlight | Integer parsed at offset `4`; `1` means on |

## Known Write or Mutating Commands

| Opcode | App Symbol | Meaning | Payload template |
| --- | --- | --- | --- |
| `0x31` | `f31597d` | Power | One byte; app template is `01`, our script uses `01` on and `00` off |
| `0x32` | `f31598e` | Reset to default/personalization settings | Empty payload; UI calls this `setRestoreFactory`, labels say "Reset to default settings" and "reset all above settings" |
| `0x34` | `f31600g` | Reset trip distance | Empty payload; UI calls this `setResetTrip` and labels it "Reset trip distance" |
| `0x35` | `f31601h` / `f31602i` | OTA/start update | Empty payload or `01` depending on flow |
| `0x36` | `O` | Sync controller time from phone / set RTC | Eight-byte payload: second, minute, hour, day-of-month, month, day-of-week, year low byte, year high byte |
| `0xd8` | `f31608o` | Set auto-lock | `01 00 00` template |
| `0xdc` | `f31610q` | Set speed independent / speed limiter type | Two-byte big-endian value; app labels `0` both, `1` PAS, `2` throttle |
| `0xe5` | `f31612s` | Set speed unit | Two-byte big-endian value; app uses `0` for `km`, `1` for `mile` |
| `0xe7` | `f31615v` | Set drive/bike gear | Two-byte template |
| `0xdb` | `f31617x` | Set handle gear | Two-byte template |
| `0xea` | `B` | Set start gear | Two-byte template |
| `0xef` | `D` | Set backlight brightness | Two-byte template |
| `0xe1` | `F` | Set max speed | Two-byte big-endian value; IMEI prefix `86` writes `round(display * 0.62137)` |
| `0xda` | `H` | Set handle PWM | Five-byte template |
| `0x49` | `J` | Set throttle sensitivity | Two-byte template |
| `0xd7` | `M` / `N` | Set anti-theft/fence | `01 00 00` enable-ish, `00 00 00` disable-ish |
| `0x42` | `Q` | Set headlight | Two-byte big-endian value; app uses `00 00` off and `00 01` on |
| `0x44` | `S` | Set ride feel | Two-byte big-endian value; labels are model-specific |
| `0xdf` | `U` | Set preset mode | `01` template |

## Present But Still Unclear

| Opcode | App Symbol | Notes |
| --- | --- | --- |
| `0x33` | `f31599f` | Command exists in `w.java`, but no visible app call site and no response parser found in `v.s3()` |
| `0xe9` | `f31618y` / `f31619z` | Read/write pair exists in `w.java`, but no visible app call site, no response parser, and no matching data-model field found. Treat as reserved/deprecated until confirmed on hardware. |

## Recovered Call-Site Evidence

- Mutating commands with no dedicated `v.s3()` parser can still complete successfully: `t.d3()` calls `s3()` and then invokes the queued listener when the response opcode matches the sent opcode.
- `0x32`: `v.u()` sends `w.f31598e`; `ui/bike/r.java` calls it from `setRestoreFactory`. The user-facing strings are `individual_restore_factory` and `individual_restore_factory_dialog_*`.
- `0x34`: `v.G()` sends `w.f31600g`; `ui/bike/o.java` calls it from `setResetTrip`. The user-facing strings are `individual_reset_trip` and `individual_resetTrip_dialog_*`.
- `0x36`: `v.t()` sends `w.O` after copying `v.g4()` into payload bytes `4..11`; `ui/main/e.java` logs success as `setTimeFromPhone`. `g4()` builds the payload from `Calendar`: seconds, minutes, 24-hour hour, day-of-month, 1-based month, Android day-of-week minus 1, and little-endian year.
- `0xe9`: only appears in `w.java`; neither `v.java` nor the visible UI/manager code sends it or parses it.

## Firmware Notes

The OTA API returns a `.vmfw` URL, but the app does not appear to unpack or
decrypt that file. `sources/bm/a.java` reads the selected file into memory and
sends it via 128-byte YMODEM packets with CRC. That means Ghidra/IDA will not be
useful until the image is decrypted, decompressed, or extracted from the target
after installation.
