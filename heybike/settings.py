import platform

SERVICE_UUID = "86531001-43e6-47b7-9cb0-5fc21d4ae340"
WRITE_UUID = "86531002-43e6-47b7-9cb0-5fc21d4ae340"
NOTIFY_UUID = "86531003-43e6-47b7-9cb0-5fc21d4ae340"
NATIVE_KEY_SECRET = "a70948d8a93b9dab:0102930405060708"
APP_VERSION = "v4.6.0"

API_BASE_URL = "https://heyapi.heybike.com/"

PHONE_TYPE = f"{platform.system()}/python:{platform.machine() or 'unknown'}"
PHONE_SYSTEMS = f"OS Version:{platform.platform()}"

LANGUAGE = "en"
COUNTRY_CODE = "US"
