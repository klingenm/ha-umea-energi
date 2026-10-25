"""Shared fixtures for Umeå Energi tests."""

import base64
import json
import time
from collections.abc import Generator
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.umea_energi.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_TYPE,
    CONF_AUTH_METHOD,
    CONF_CUSTOMER_CODE,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRES_AT,
    DOMAIN,
    AccountType,
    AuthMethod,
)

CUSTOMER_CODE = "123456"


def make_jwt(expires_in: float = 3600) -> str:
    """An unsigned JWT that expires `expires_in` seconds from now."""

    def encode(part: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode({'exp': time.time() + expires_in})}."


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: object, enable_custom_integrations: None
) -> None:
    """Load the integration from custom_components; the recorder is a dependency."""


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Skip setting up the config entry created by a flow."""
    with patch("custom_components.umea_energi.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A configured Umeå Energi customer."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Umeå Energi {CUSTOMER_CODE}",
        unique_id=CUSTOMER_CODE,
        data={
            CONF_ACCOUNT_TYPE: AccountType.PRIVATE,
            CONF_AUTH_METHOD: AuthMethod.BANKID,
            CONF_CUSTOMER_CODE: CUSTOMER_CODE,
            CONF_ACCESS_TOKEN: make_jwt(),
            CONF_REFRESH_TOKEN: "refresh-token",
            CONF_REFRESH_TOKEN_EXPIRES_AT: None,
        },
    )
