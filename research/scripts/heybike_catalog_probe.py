#!/usr/bin/env python3
"""
Inspect HeyBike/YuLai bike model and color catalog endpoints.

This is API-only and read-only. It logs in, calls the catalog endpoints, and
optionally compares them with the account bike metadata returned by getUserBikes.
"""

from __future__ import annotations

import argparse
import getpass
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any
from urllib import parse, request


API_BASE_URL = "https://heyapi.heybike.com/"
APP_VERSION = "v4.6.0"
PHONE_TYPE = f"{platform.system()}/python:{platform.machine() or 'unknown'}"
PHONE_SYSTEMS = f"OS Version:{platform.platform()}"
LANGUAGE = "en"
COUNTRY_CODE = "US"
KNOWN_COLOR_FIELDS = {
    "addTime",
    "bigImg",
    "cname",
    "coId",
    "color",
    "cvalue",
    "cvalue1",
    "cvalue2",
    "cvalue3",
    "deType",
    "isSelect",
    "name",
    "sideImg",
    "smallImg",
}


class CatalogProbeError(RuntimeError):
    pass


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
        raise CatalogProbeError(f"API returned non-JSON from {endpoint}: {raw[:200]}") from exc

    status = parsed.get("status")
    ok_statuses = {None, 0, 1, 200, "0", "1", "200"}
    if status not in ok_statuses:
        message = parsed.get("message") or parsed.get("msg") or parsed
        raise CatalogProbeError(f"API {endpoint} failed with status {status}: {message}")
    return parsed


def api_login(email: str, password: str, timeout: float) -> str:
    data = api_post(
        "appHeyApi/login",
        {
            "userEmail": email,
            "phoneInfo": APP_VERSION,
            "phoneType": PHONE_TYPE,
            "phoneSystems": PHONE_SYSTEMS,
            "userPass": password,
        },
        timeout=timeout,
    )
    token = str(data.get("token") or "")
    if not token:
        raise CatalogProbeError(f"login succeeded but no token was returned: {redacted_json(data)}")
    return token


def get_user_bikes(token: str, timeout: float) -> dict[str, Any]:
    return api_post("appHeyApi/getUserBikes", {"token": token}, timeout=timeout)


def get_all_bike_types(token: str, timeout: float) -> dict[str, Any]:
    return api_post("appHeyApi/getAllBikeType", {"token": token}, timeout=timeout)


def get_all_bike_color_type(token: str, de_type: str, timeout: float) -> dict[str, Any]:
    return api_post(
        "appHeyApi/getAllBikeColorType",
        {"token": token, "deType": str(de_type)},
        timeout=timeout,
    )


def get_bike_by_imei(token: str, imei: str, timeout: float) -> dict[str, Any]:
    return api_post(
        "appHeyApi/getBikeByIMEI",
        {"token": token, "deIMEI": str(imei)},
        timeout=timeout,
    )


def is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return "token" in lower or "password" in lower or lower.endswith("key") or "blekey" in lower


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if is_sensitive_key(str(key)) and item else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def redacted_json(value: Any) -> str:
    return json.dumps(redact(value), indent=2, sort_keys=True)


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def extract_list(data: Any, key: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    value = data.get(key)
    if isinstance(value, list):
        return value
    nested = data.get("data")
    if isinstance(nested, dict):
        value = nested.get(key)
        if isinstance(value, list):
            return value
    return []


def first_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return ""


def first_int_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if value is None or str(value) == "":
            continue
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)
    return ""


def parse_de_type_args(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in value.split(","):
            de_type = part.strip()
            if not de_type or de_type in seen:
                continue
            seen.add(de_type)
            out.append(de_type)
    return out


def parse_text_args(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in value.split(","):
            item = part.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            out.append(item)
    return out


def summarize_account_bike(item: dict[str, Any]) -> dict[str, str]:
    bike_type = as_dict(item.get("bikeType"))
    bike_color = as_dict(item.get("bikeColor"))
    return {
        "name": first_text(item, "nickName", "name") or first_text(bike_type, "typeName", "bikeType"),
        "imei": first_text(item, "deIMEI", "imei"),
        "ble": first_text(item, "deBle", "bleMac", "macAddress"),
        "de_type": first_int_text(item, "deType") or first_int_text(bike_type, "typeId", "id"),
        "model_id": first_int_text(bike_type, "typeId", "id"),
        "model_name": first_text(bike_type, "typeName", "bikeType"),
        "color_id": first_int_text(bike_color, "coId") or first_int_text(item, "color"),
        "color_name": first_text(bike_color, "cname"),
        "color_hex": ";".join(
            part
            for part in [
                first_text(bike_color, "cvalue1"),
                first_text(bike_color, "cvalue2"),
                first_text(bike_color, "cvalue3"),
            ]
            if part
        ),
    }


def summarize_bike_info(imei: str, response_data: dict[str, Any]) -> dict[str, str]:
    bike = as_dict(response_data.get("bike"))
    bike_type = as_dict(response_data.get("bikeType")) or as_dict(bike.get("bikeType"))
    bike_color = as_dict(response_data.get("bikeColor")) or as_dict(bike.get("bikeColor"))
    return {
        "imei": first_text(bike, "deIMEI", "imei") or imei,
        "de_type": first_int_text(bike, "deType") or first_int_text(bike_type, "typeId", "id"),
        "model_id": first_int_text(bike_type, "typeId", "id"),
        "model_name": first_text(bike_type, "typeName", "bikeType"),
        "color_id": first_int_text(bike_color, "coId") or first_int_text(bike, "color"),
        "color_name": first_text(bike_color, "cname"),
        "color_hex": ";".join(
            part
            for part in [
                first_text(bike_color, "cvalue1"),
                first_text(bike_color, "cvalue2"),
                first_text(bike_color, "cvalue3"),
            ]
            if part
        ),
    }


def summarize_bike_type(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": first_int_text(item, "typeId", "id", "deType"),
        "name": first_text(item, "typeName", "bikeType"),
        "max_speed": first_int_text(item, "maxSpeed"),
        "gear_num": first_int_text(item, "gearNum"),
        "battery": format_battery(item),
        "speed_unit": first_int_text(item, "speedUnit"),
        "category": first_int_text(item, "vehicleCategory", "vehicleCategoryId", "bikeCategory"),
    }


def format_battery(item: dict[str, Any]) -> str:
    voltage = first_int_text(item, "batteryVoltage")
    capacity = first_int_text(item, "batteryCapacity")
    if voltage and capacity:
        return f"{voltage}V/{capacity}Ah"
    if voltage:
        return f"{voltage}V"
    if capacity:
        return f"{capacity}Ah"
    return ""


def summarize_color(item: dict[str, Any]) -> dict[str, str]:
    extra = {
        key: value
        for key, value in item.items()
        if key not in KNOWN_COLOR_FIELDS and value not in (None, "")
    }
    return {
        "de_type": first_int_text(item, "deType"),
        "co_id": first_int_text(item, "coId", "color"),
        "cname": first_text(item, "cname", "name"),
        "cvalue": first_text(item, "cvalue"),
        "cvalue1": first_text(item, "cvalue1"),
        "cvalue2": first_text(item, "cvalue2"),
        "cvalue3": first_text(item, "cvalue3"),
        "big_img": first_text(item, "bigImg"),
        "small_img": first_text(item, "smallImg"),
        "side_img": first_text(item, "sideImg"),
        "add_time": first_text(item, "addTime"),
        "is_select": first_text(item, "isSelect"),
        "extra": json.dumps(extra, sort_keys=True, separators=(",", ":")) if extra else "",
    }


def print_table(title: str, rows: list[dict[str, str]], columns: list[str]) -> None:
    print(f"\n{title}")
    if not rows:
        print("  <none>")
        return
    widths = {
        column: max(len(column), *(len(row.get(column, "")) for row in rows))
        for column in columns
    }
    header = "  " + "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("  " + "  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  " + "  ".join(row.get(column, "").ljust(widths[column]) for column in columns))


def collect_account_de_types(user_bikes_response: dict[str, Any]) -> list[str]:
    de_types: list[str] = []
    seen: set[str] = set()
    for item in extract_list(user_bikes_response, "bikes"):
        if not isinstance(item, dict):
            continue
        summary = summarize_account_bike(item)
        de_type = summary.get("de_type", "")
        if de_type and de_type not in seen:
            seen.add(de_type)
            de_types.append(de_type)
    return de_types


def collect_account_imeis(user_bikes_response: dict[str, Any]) -> list[str]:
    imeis: list[str] = []
    seen: set[str] = set()
    for item in extract_list(user_bikes_response, "bikes"):
        if not isinstance(item, dict):
            continue
        imei = first_text(item, "deIMEI", "imei")
        if imei and imei not in seen:
            seen.add(imei)
            imeis.append(imei)
    return imeis


def collect_type_ids(types_response: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for item in extract_list(types_response, "bikeTypes"):
        if not isinstance(item, dict):
            continue
        type_id = summarize_bike_type(item).get("id", "")
        if type_id and type_id not in seen:
            seen.add(type_id)
            ids.append(type_id)
    return ids


def collect_type_names(types_response: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in extract_list(types_response, "bikeTypes"):
        if not isinstance(item, dict):
            continue
        summary = summarize_bike_type(item)
        type_id = summary.get("id", "")
        name = summary.get("name", "")
        if type_id and name:
            names[type_id] = name
    return names


def print_summary(results: dict[str, Any]) -> None:
    user_bikes = results.get("getUserBikes")
    if isinstance(user_bikes, dict):
        bike_rows = [
            summarize_account_bike(item)
            for item in extract_list(user_bikes, "bikes")
            if isinstance(item, dict)
        ]
        print_table(
            "Account Bikes",
            bike_rows,
            ["name", "de_type", "model_id", "model_name", "color_id", "color_name", "color_hex"],
        )

    bike_info_results = results.get("getBikeByIMEI")
    if isinstance(bike_info_results, dict):
        rows = [
            summarize_bike_info(imei, response_data)
            for imei, response_data in bike_info_results.items()
            if isinstance(response_data, dict)
        ]
        print_table(
            "Bike Info By IMEI",
            rows,
            ["imei", "de_type", "model_id", "model_name", "color_id", "color_name", "color_hex"],
        )

    type_names: dict[str, str] = {}
    types = results.get("getAllBikeType")
    if isinstance(types, dict):
        type_names = collect_type_names(types)
        type_rows = [
            summarize_bike_type(item)
            for item in extract_list(types, "bikeTypes")
            if isinstance(item, dict)
        ]
        print_table(
            "All Bike Types",
            type_rows,
            ["id", "name", "max_speed", "gear_num", "battery", "speed_unit", "category"],
        )

    color_results = results.get("getAllBikeColorType")
    if isinstance(color_results, dict):
        for de_type, response_data in color_results.items():
            color_rows = [
                summarize_color(item)
                for item in extract_list(response_data, "bikeTypes")
                if isinstance(item, dict)
            ]
            title = f"Colors For deType={de_type}"
            type_name = type_names.get(str(de_type))
            if type_name:
                title += f" ({type_name})"
            print_table(
                title,
                color_rows,
                [
                    "de_type",
                    "co_id",
                    "cname",
                    "cvalue",
                    "cvalue1",
                    "cvalue2",
                    "cvalue3",
                    "big_img",
                    "small_img",
                    "side_img",
                    "add_time",
                    "is_select",
                    "extra",
                ],
            )


def write_output(path: Path, data: dict[str, Any], *, no_redact: bool) -> None:
    payload = data if no_redact else redact(data)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nWrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect HeyBike model/color catalog API responses."
    )
    parser.add_argument("--email", help="HeyBike account email.")
    parser.add_argument("--password", help="HeyBike password. Omit to prompt.")
    parser.add_argument("--api-token", help="Existing HeyBike API token; skips login.")
    parser.add_argument("--skip-user-bikes", action="store_true", help="Do not call getUserBikes.")
    parser.add_argument("--skip-types", action="store_true", help="Do not call getAllBikeType.")
    parser.add_argument(
        "--color-de-type",
        action="append",
        default=[],
        help="deType/model id to pass to getAllBikeColorType. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--all-colors",
        action="store_true",
        help="Deprecated no-op; colors for all getAllBikeType results are now fetched by default.",
    )
    parser.add_argument(
        "--skip-type-colors",
        action="store_true",
        help="Do not call getAllBikeColorType for every id returned by getAllBikeType.",
    )
    parser.add_argument(
        "--no-account-colors",
        action="store_true",
        help="Do not call getAllBikeColorType for deTypes found in getUserBikes.",
    )
    parser.add_argument(
        "--bike-imei",
        action="append",
        default=[],
        help="IMEI to pass to getBikeByIMEI. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--account-bike-info",
        action="store_true",
        help="Call getBikeByIMEI for IMEIs found in getUserBikes.",
    )
    parser.add_argument("--raw", action="store_true", help="Print full JSON responses after summaries.")
    parser.add_argument("--output", type=Path, help="Write collected JSON to this file.")
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help="Do not redact tokens or BLE keys in raw/output JSON.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=0.1, help="Delay between color catalog requests.")
    args = parser.parse_args()

    if not args.email and not args.api_token:
        parser.error("provide --email or --api-token")
    if args.all_colors and args.skip_types:
        parser.error("--all-colors requires getAllBikeType; remove --skip-types")

    try:
        token = args.api_token
        if not token:
            password = args.password
            if password is None:
                password = getpass.getpass(f"HeyBike password for {args.email}: ")
            print("Logging in to HeyBike API...")
            token = api_login(args.email, password, args.timeout)

        results: dict[str, Any] = {}
        user_bikes_response: dict[str, Any] | None = None
        types_response: dict[str, Any] | None = None

        if not args.skip_user_bikes:
            print("Calling appHeyApi/getUserBikes...")
            user_bikes_response = get_user_bikes(token, args.timeout)
            results["getUserBikes"] = user_bikes_response

        if not args.skip_types:
            print("Calling appHeyApi/getAllBikeType...")
            types_response = get_all_bike_types(token, args.timeout)
            results["getAllBikeType"] = types_response

        imeis = parse_text_args(args.bike_imei)
        if user_bikes_response is not None and args.account_bike_info:
            imeis.extend(collect_account_imeis(user_bikes_response))

        deduped_imeis: list[str] = []
        seen_imeis: set[str] = set()
        for imei in imeis:
            if imei in seen_imeis:
                continue
            seen_imeis.add(imei)
            deduped_imeis.append(imei)

        if deduped_imeis:
            results["getBikeByIMEI"] = {}
        for index, imei in enumerate(deduped_imeis):
            if index:
                time.sleep(max(0.0, args.delay))
            print(f"Calling appHeyApi/getBikeByIMEI deIMEI={imei}...")
            results["getBikeByIMEI"][imei] = get_bike_by_imei(token, imei, args.timeout)

        de_types = parse_de_type_args(args.color_de_type)
        if user_bikes_response is not None and not args.no_account_colors:
            de_types.extend(collect_account_de_types(user_bikes_response))
        if types_response is not None and not args.skip_type_colors:
            de_types.extend(collect_type_ids(types_response))

        deduped_de_types: list[str] = []
        seen_de_types: set[str] = set()
        for de_type in de_types:
            if de_type in seen_de_types:
                continue
            seen_de_types.add(de_type)
            deduped_de_types.append(de_type)

        if deduped_de_types:
            results["getAllBikeColorType"] = {}
        for index, de_type in enumerate(deduped_de_types):
            if index:
                time.sleep(max(0.0, args.delay))
            print(f"Calling appHeyApi/getAllBikeColorType deType={de_type}...")
            results["getAllBikeColorType"][de_type] = get_all_bike_color_type(
                token,
                de_type,
                args.timeout,
            )

        print_summary(results)

        if args.raw:
            print("\nRaw JSON")
            payload = results if args.no_redact else redact(results)
            print(json.dumps(payload, indent=2, sort_keys=True))

        if args.output:
            write_output(args.output, results, no_redact=args.no_redact)

        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
