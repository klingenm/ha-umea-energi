"""Constants for the Umeå Energi integration."""

import logging
from datetime import timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

DOMAIN = "umea_energi"
LOGGER = logging.getLogger(__package__)

BASE_URL = "https://tapi.umeaenergi.se"
API_VERSION = "2"
# Naive timestamps from the API are local Swedish time.
API_TIMEZONE = ZoneInfo("Europe/Stockholm")

CONF_ACCOUNT_TYPE = "account_type"
CONF_AUTH_METHOD = "auth_method"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_CODE = "customer_code"
CONF_ACTIVATION_KEY = "activation_key"
CONF_UPDATE_INTERVAL = "update_interval"

DEFAULT_UPDATE_INTERVAL = 60  # minutes
MIN_UPDATE_INTERVAL = 15
MAX_UPDATE_INTERVAL = 24 * 60

# How far back to import hourly statistics the first time a meter is seen.
INITIAL_HISTORY = timedelta(days=30)
# Re-import this much already imported history on every update, so late or corrected readings
# replace earlier values.
STATISTICS_REIMPORT_WINDOW = timedelta(days=2)


class AccountType(StrEnum):
    """Whether the login is for a private or a business customer."""

    PRIVATE = "private"
    BUSINESS = "business"


class AuthMethod(StrEnum):
    """How the user signs in to Umeå Energi."""

    BANKID = "bankid"
    EMAIL = "email"


class Utility(StrEnum):
    """Metered utilities exposed by the integration."""

    ELECTRICITY = "electricity"
    DISTRICT_HEATING = "district_heating"
