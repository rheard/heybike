#!/usr/bin/env python3
"""
Check whether HeyBike's getBikeByBleMac endpoint leaks a BLE key to an
unassociated account.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from heybike_power import (
    AccountBike,
    ProtocolError,
    api_bike_by_ble_mac,
    api_login,
    api_user_bikes,
    choose_account_bike,
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


def owns_mac(bikes: list[AccountBike], mac: str) -> bool:
    wanted = compact_mac(mac)
    return any(compact_mac(bike.mac) == wanted for bike in bikes)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely test whether a second HeyBike account can fetch the BLE key "
            "for one bike selected from your owner account."
        )
    )
    parser.add_argument("--owner-email", help="Email for the account that owns the bike.")
    parser.add_argument("--owner-password", help="Owner account password. Omit to prompt.")
    parser.add_argument("--owner-token", help="Existing API token for the owner account.")
    parser.add_argument("--test-email", help="Email for an unassociated test account.")
    parser.add_argument("--test-password", help="Test account password. Omit to prompt.")
    parser.add_argument("--test-token", help="Existing API token for the test account.")
    parser.add_argument("--bike-index", type=int, help="Owner-account bike index to test.")
    parser.add_argument("--bike-name", help="Owner-account bike name substring to test.")
    parser.add_argument("--bike-mac", help="Owner-account bike MAC to test.")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--show-key-fingerprint",
        action="store_true",
        help="Print only short encrypted-key fingerprints, never full keys.",
    )
    args = parser.parse_args()

    try:
        owner_token = account_token(
            "owner", args.owner_email, args.owner_password, args.owner_token
        )
        owner_bikes = api_user_bikes(owner_token)
        selected = choose_account_bike(
            owner_bikes,
            requested_mac=args.bike_mac,
            requested_name=args.bike_name,
            requested_index=args.bike_index,
            non_interactive=args.non_interactive,
        )
        print(f"Selected owned bike: {selected.name}  {selected.mac}")

        test_token = account_token("test", args.test_email, args.test_password, args.test_token)
        test_bikes = api_user_bikes(test_token)
        if owns_mac(test_bikes, selected.mac):
            raise ProtocolError(
                "The test account already has this bike in getUserBikes; "
                "that does not test cross-account access control."
            )

        print("Calling getBikeByBleMac from the unassociated test account...")
        leaked = api_bike_by_ble_mac(test_token, selected.mac)
        if not leaked.encrypted_ble_key:
            print("PASS: endpoint returned no BLE key.")
            return 0

        print("FAIL: unassociated test account received a BLE key for the owned bike.")
        print("This is a server-side access-control issue if the test account is truly unaffiliated.")
        if args.show_key_fingerprint:
            owner_key = selected.encrypted_ble_key
            leaked_key = leaked.encrypted_ble_key
            print(f"Owner key fingerprint: {owner_key[:6]}...{owner_key[-6:]} ({owner_key})")
            print(f"Test  key fingerprint: {leaked_key[:6]}...{leaked_key[-6:]} ({leaked_key})")
            print(f"Same encrypted key: {owner_key == leaked_key}")
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
