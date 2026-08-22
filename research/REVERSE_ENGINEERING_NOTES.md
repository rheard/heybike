# HeyBike Reverse Engineering Notes

Scope: this is based on the exported/decompiled Java source in this workspace. The full source dump now includes the previously missing Java packages `vl`, `am`, `bm`, and `zl`, plus the native resources from the split APK. The native constants exposed through `NativeLib` have been recovered from `config_resources/lib/arm64-v8a/libbikeKey.so`.

## High-level findings

- The app has a normal BLE control path for bike commands and a separate OTA firmware update flow.
- The bike-on/off command is visible and straightforward once connected: command opcode `0x31`, one-byte payload `0x01` for on and `0x00` for off.
- Normal BLE commands are framed as 16-byte packets starting with ASCII `a b`, then encrypted before being written.
- The app fetches a short live BLE token after connecting and injects that token into bytes 12-15 of later commands.
- The MAC address is not the BLE password. The app uses a separate server-provided `bleKey`, then transforms that key through `vl.a`.
- A Python recreation of the BLE power flow has been added at `tools/heybike_power.py`.
- A read-only BLE opcode probe has been added at `tools/heybike_ble_probe.py`.
- A focused headlight read/write script has been added at `tools/heybike_headlight.py`.
- A focused max-speed read/write script has been added at `tools/heybike_max_speed.py`.
- Focused speed-unit and ride-feel scripts have been added at `tools/heybike_speed_unit.py` and `tools/heybike_ride_feel.py`.
- A focused speed-limiter type script has been added at `tools/heybike_speed_independent.py`.
- A YMODE firmware update script has been added at `tools/heybike_firmware_update.py`.
- A working opcode table has been added at `PROTOCOL_OPCODE_MAP.md`.
- Firmware update evidence is present. The main screen is `BikeOtaActivity`, backed by a viewmodel that checks the server for available firmware and either sends firmware over BLE or asks the server/bike to update by FTP.
- The downloaded `.vmfw` appears high-entropy and has no obvious firmware header/vector table. The app sends it raw over YMODEM, so decryption or decompression likely happens in the bike bootloader.
- I did not find evidence in the Java dump of continuous microphone recording or audio capture.
- The app does collect/use location for ride tracking and has analytics, push notifications, Firebase Crashlytics, and verbose HTTP/BLE logging.
- The most concerning visible behavior is logging sensitive network parameters and BLE traffic, including tokens/IMEIs/command data, through app logs.

## Key files

- `heybike/bluetooth/t.java`: abstract BLE client. Handles connection, GATT service/characteristic discovery, token injection, encryption/decryption, notification handling, and write queueing.
- `heybike/bluetooth/v.java`: concrete BLE command client. Defines power, settings, status parsing, OTA command startup, and OTA packet forwarding.
- `heybike/bluetooth/z.java`: singleton BLE connection manager used by UI/managers.
- `heybike/bluetooth/w.java`: static BLE command table.
- `heybike/data/type/BleCmdBean.java`: wraps one or more command frames.
- `heybike/data/type/BleCmdPartBean.java`: builds each normal 16-byte BLE command frame.
- `heybike/ui/bike/BikeOtaActivity.java`: OTA/update UI.
- `heybike/ui/bike/n.java`: OTA viewmodel and most firmware update flow logic.
- `heybike/data/type/OTAUpgrade.java`: server response model for firmware checks.
- `heybike/data/type/OTAMode.java`: OTA modes: `LIANZHAO` and `YMODE`.
- `heybike/net/b.java`: Retrofit API endpoint definitions.
- `heybike/net/a.java`: API repository wrapper.
- `heybike/net/d.java`: Retrofit/OkHttp setup and logging interceptors.
- `heybike/service/RideLocationService.java`: foreground location service.
- `heybike/ui/App.java`: app startup, analytics, push, Crashlytics.
- `heybike/manager/m.java`: TalkingData analytics configuration.
- `config_resources/lib/arm64-v8a/libbikeKey.so`: native crypto constants used by `NativeLib`.
- `PROTOCOL_OPCODE_MAP.md`: current opcode table derived from `w.java` and `v.s3()`.

## BLE service and characteristics

Normal BLE UUIDs are defined in `heybike/bluetooth/t.java`:

- Service: `86531001-43e6-47b7-9cb0-5fc21d4ae340`
- Write characteristic: `86531002-43e6-47b7-9cb0-5fc21d4ae340`
- Notify characteristic: `86531003-43e6-47b7-9cb0-5fc21d4ae340`

`t.u0(BluetoothGatt)` locates those GATT objects. `t.o0()` enables notifications on the normal notify characteristic.

OTA can switch to different write/notify characteristics for `LIANZHAO` mode through UUIDs held in missing package `am.a`.

## Normal BLE frame format

`BleCmdPartBean` constructs each normal command as a 16-byte frame:

```text
byte 0    0x61  ASCII 'a'
byte 1    0x62  ASCII 'b'
byte 2    opcode / command type
byte 3    payload length
byte 4..  payload bytes
byte 12..15 live 4-byte token, inserted before send
```

Before a command is written, `t.V3(byte[])` copies the current token into bytes 12-15. `t.R3()` then encrypts the command via:

```java
vl.a.f55393a.d(commandBytes, bikeKey)
```

Incoming notifications are decrypted in `t.d3()` via:

```java
vl.a.f55393a.a(notificationBytes, bikeKey)
```

The Java implementation of `vl.a` is now present:

- BLE command encrypt/decrypt uses `Cipher.getInstance(NativeLib.getCryptTransformation())`.
- The BLE command key is the transformed/plain BLE key string encoded as UTF-8.
- The native `libbikeKey.so` strings confirm the command transformation is `AES/ECB/NoPadding`.
- Server `bleKey` transform/decrypt uses `Cipher.getInstance(NativeLib.getCryptTransformation2())` with key and IV split from `NativeLib.getKeySecret()` by `:`.
- Native constants recovered from `config_resources/lib/arm64-v8a/libbikeKey.so`:
  - `getCryptAlgorithm()` -> `AES`
  - `getCryptTransformation()` -> `AES/ECB/NoPadding`
  - `getCryptTransformation2()` -> `AES/CBC/PKCS5Padding`
  - `getKeySecret()` -> `a70948d8a93b9dab:0102930405060708`

## BLE connect/token flow

After GATT setup, `t.N2()` requests a BLE token unless one is already cached. It sends command `w.f31595b`. When a notification decrypts to the expected token response prefix, `t.d3()` copies response bytes 4-7 into the client token field. Later commands reuse that token in bytes 12-15.

Bike password/key handling appears in `heybike/bluetooth/x.java`, which normalizes the MAC and transforms the supplied bike password/key using `vl.a.f55393a.b(password)`.

The source of that password/key is visible:

- `heybike/manager/f.java` builds the BLE credential object from `BikeUsingBean.getBleMac()` and `BikeUsingBean.getBleKey()`.
- `heybike/manager/f.java` stores `BikeUsingBean.bleKey` from `netBike.getBleKey()`.
- `heybike/ui/bind/b.java` fetches BLE keys while binding through `getBikeBleKey` and `getBikeByBleMac`.
- `heybike/net/b.java` defines those endpoints as `appHeyApi/getBikeBleKey` and `appHeyApi/getBikeByBleMac`.

There is also `sources/com/yulai/heybike/utils/NativeLib.java`, which loads native library `bikeKey` and exposes:

```java
getCryptAlgorithm()
getCryptTransformation()
getCryptTransformation2()
getKeySecret()
```

That native library provides the strings needed to decrypt a server-provided `bleKey`.

## Turn bike on/off

UI path:

```text
DashboardFragment
  -> heybike/ui/main/e.java P0()
  -> heybike/manager/f.java i0(...)
  -> heybike/bluetooth/z.java p(...)
  -> heybike/bluetooth/v.java p(...)
```

BLE command:

- Command object: `w.f31597d`
- Opcode: `0x31`
- Payload length: `1`
- Payload: `0x01` for on, `0x00` for off

Before token insertion and encryption, the logical frames are:

```text
Power on:
61 62 31 01 01 00 00 00 00 00 00 00 TT TT TT TT

Power off:
61 62 31 01 00 00 00 00 00 00 00 00 TT TT TT TT
```

`TT TT TT TT` is the live token from the token fetch step. The frame then gets encrypted before write, so these exact bytes are not what goes over BLE unless the encryption layer is disabled or reproduced.

Response parsing for opcode `0x31` is in `v.s3()`. It checks response byte 4, updates cached power state, updates `BikeBleBaseInfo.powerStatus`, and logs either `power-on` or `power-off`.

Network fallback:

If BLE is not connected but the bike/account is in a supported online mode, `manager.f.i0(...)` can call the API endpoint:

```text
POST appHeyApi/openCloseBike
fields: token, deIMEI, openType
```

## Firmware update flow

Firmware update evidence is definitely present.

Entry point:

```text
heybike/ui/bike/BikeOtaActivity.java
  -> heybike/ui/bike/n.java
```

The activity requires an IMEI intent extra. The viewmodel reads current BLE bike info, then checks the server for update metadata:

```text
POST appHeyApi/getBikeIMEIUpload
fields: token, deIMEI, hardVersion, oldVersion
returns: OTAUpgrade
```

Important `OTAUpgrade.IotInfo` fields:

- `otaVersion`
- `otaHardVersion`
- `otaType`
- `otaUrl`
- `otaUrllz`
- `ftpUrl`
- `mustUpgrade`
- `upgradeInstructions`

The visible update check considers an update available when `iotInfo.otaUrl` is non-empty. It displays the new version as:

```text
otaHardVersion.otaVersion
```

### BLE OTA path

`n.t0()` starts upgrade. If there is no usable FTP flow, it downloads `otaUrl` and then calls:

```text
z.R(otaMode, downloadedFilePath, listener)
```

The OTA mode and local firmware filename are selected from the bike type/source:

- Bike type starts with `88`: `OTAMode.LIANZHAO`, file `upgradeFile_<otaVersion>.bin`
- Bike type starts with `86` or `85`: `OTAMode.YMODE`, file `app.bin`
- Otherwise: `OTAMode.YMODE`, file `upgradeFile_<otaVersion>.vmfw`

`z.R(...)` configures the BLE client OTA mode, requests MTU, and calls `v.a(filePath, listener)`.

`v.a(...)`:

- Clears queued normal commands.
- Starts an OTA timeout/progress timer.
- Loads the firmware into the selected OTA protocol engine.
- Sends normal encrypted command `w.f31601h`.

`w.f31601h` is opcode `0x35` with no payload. This appears to be the "enter/start OTA" command.

For `LIANZHAO` mode:

- On successful `0x35` response, it switches OTA characteristics through `J2(...)`.
- It writes `X2().onStart()` data.
- It sends chunks using `n4()`.

For `YMODE` mode:

- On successful `0x35` response, it marks OTA as started.
- After 2 seconds, `m4()` sends `w.f31602i`, which is also opcode `0x35` with one-byte payload `0x01`.
- Raw OTA responses are handled by `r3()`, which calls `X2().f(responseBytes)` to produce the next bytes to write.

The OTA protocol engine is selected by `t.X2()`:

- `LIANZHAO`: `am.a.f641a`
- `YMODE`: `bm.a.f19961a`

The full source now includes those engines:

- `bm.a` implements a YMODEM-like 128-byte block protocol using SOH (`0x01`), EOT (`0x04`), ACK (`0x06`), NAK (`0x15`), and CRC16.
- `am.a` implements the LIANZHAO mode, using service `02f00000-0000-0000-0000-00000000fe00`, write characteristic `02f00000-0000-0000-0000-00000000ff01`, notify characteristic `02f00000-0000-0000-0000-00000000ff02`, 247-byte MTU, 4096-byte header/offset steps, and 235-byte body chunks.

### FTP OTA path

If `iotInfo.ftpUrl` exists and `isFailFtp < 1`, the app does not send firmware over BLE. It calls:

```text
POST appHeyApi/uploadByFtp
fields: token, deIMEI, openType, ftpUrl
```

The UI then marks progress as complete. This looks like a server/cellular-bike update trigger rather than app-to-bike BLE transfer.

### OTA result reporting

The viewmodel reports OTA success/failure to:

```text
POST appHeyApi/doUploadBike
fields: token, deIMEI, openType, failType, failReasopn
```

The misspelled field name `failReasopn` is in the app code.

There is also an `otaMonitor/report` endpoint in `heybike/net/b.java`, but I did not fully trace it into the main OTA path.

## Security and privacy notes

### No visible always-on microphone capture

I searched the Java dump for microphone/audio capture indicators such as:

- `RECORD_AUDIO`
- `Manifest.permission.RECORD_AUDIO`
- `MediaRecorder`
- `AudioRecord`
- `startRecording`

I did not find evidence of audio recording in the visible Java source. This is not a complete proof for the original APK because manifest entries, native libraries, and packaged dependencies were not reviewed.

### Location is present

The app uses foreground location tracking through `RideLocationService` and Google/Fused Location APIs. Permission constants in visible source include:

- `ACCESS_FINE_LOCATION`
- `ACCESS_COARSE_LOCATION`

This looks tied to ride tracking/maps rather than hidden microphone-like behavior.

### Analytics, crash reporting, and push

`App.onCreate()` initializes:

- TalkingData analytics
- Aliyun push
- Firebase Crashlytics

`manager/m.java` configures TalkingData with several sensitive collection flags disabled:

- IMEI/MEID disabled
- MAC disabled
- app list disabled
- location disabled

The app still logs events and page views through TalkingData.

Aliyun push registers the device and uses an alias derived from a hashed/lowercased email. Crashlytics records uncaught exceptions.

### Verbose logging of sensitive data

This is the strongest visible concern.

`heybike/net/d.java` installs an OkHttp `HttpLoggingInterceptor` at `BODY` level and also has a custom interceptor that logs:

- URL/name
- request timing
- HTTP code
- form parameters

Since many endpoints send `token`, `deIMEI`, email, firmware URLs, and control parameters in form bodies, these values can appear in logs.

The BLE layer also logs raw-ish command/response data, including write data, received command data, and token acquisition. If logs are uploadable or accessible via logcat/debug builds, that can leak bike identifiers, auth tokens, BLE command tokens, and command history.

### Firmware validation is not visible

The viewmodel accepts a downloaded firmware file if the path exists and the file length is greater than zero. I did not find visible signature or hash verification before sending it to the bike.

Validation might exist inside the missing OTA engines (`am.a`, `bm.a`) or in the bike bootloader. From the visible app code alone, the firmware trust model is mostly "server returned this URL, app downloaded non-empty file".

### PDF viewer leaks URLs to Google

`PDFViewActivity` loads PDFs through:

```text
https://docs.google.com/gview?url=<encoded>&embedded=true
```

That sends the PDF URL to Google. This is not necessarily malicious, but it is a privacy detail.

## Suggested renames

If cleaning this code up, these names would make the reverse-engineered structure easier to work with:

- `heybike/bluetooth/t.java` -> `BleClientBase`
- `heybike/bluetooth/v.java` -> `HeyBikeBleClient`
- `heybike/bluetooth/z.java` -> `BleConnectionManager`
- `heybike/bluetooth/w.java` -> `BleCommandRegistry`
- `heybike/bluetooth/x.java` -> `BikeBleCredentials`
- `heybike/ui/bike/n.java` -> `BikeOtaViewModel`
- `heybike/net/b.java` -> `HeyBikeApi`
- `heybike/net/a.java` -> `HeyBikeApiRepository`
- `heybike/net/d.java` -> `NetworkClientFactory`
- `heybike/manager/f.java` -> `BikeControlManager`

## Python Power Script

Added:

```text
tools/heybike_power.py
tools/README_HEYBIKE_POWER.md
```

The script implements:

- Login to `https://heyapi.heybike.com/`.
- Account bike lookup through `appHeyApi/getUserBikes`.
- Decryption of the server-provided `bleKey` using recovered native constants.
- App-like BLE scanning that keeps devices named `Heybike*` or advertising the expected service UUID.
- BLE scan/connect with `bleak`.
- AES command encryption/decryption.
- Token fetch command `16 5A 01 00 ...`.
- Power opcode `0x31`, payload `0x01` or `0x00`.
- Notification parsing for the app's `{ encrypted-16-bytes }` response envelope.

It can still run with a manual transformed/plain BLE key or encrypted server `bleKey`, but the intended path is `--email` plus a password prompt.

## Missing pieces to recover next

Remaining gaps:

- Live BLE testing against the bike.
- Handling any server-side login edge case such as email OTP challenge, if the server requires it for this account/session.
- OTA firmware update as a Python implementation.

The power script should now have the pieces needed for the normal account-to-bike BLE power path.

## Firmware Acquisition Script

Added:

```text
tools/heybike_firmware_download.py
```

Visible app behavior does not include a current-firmware readback/dump command. The OTA acquisition path is:

1. Read current BLE base info with opcode `0x38`.
2. Parse current IOT hard/version from response bytes 8 and 9.
3. Call `POST appHeyApi/getBikeIMEIUpload` with `token`, `deIMEI`, `hardVersion`, and `oldVersion`.
4. If `iotInfo.otaUrl` is present, download it.

The downloader recreates that path and writes the OTA metadata JSON next to the firmware file. It does not flash firmware.
