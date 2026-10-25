"""Tests for the Umeå Energi config flow."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from umea_energi_api_client import (
    ApiError,
    BankIdProgress,
    Credentials,
    CustomerType,
    InvalidCredentialsError,
    Login,
    UmeaEnergiClient,
    User,
)

from custom_components.umea_energi.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_TYPE,
    CONF_ACTIVATION_KEY,
    CONF_AUTH_METHOD,
    CONF_CUSTOMER_CODE,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRES_AT,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    AccountType,
    AuthMethod,
)

from .conftest import CUSTOMER_CODE


def make_login(customer_code: str | None = CUSTOMER_CODE, token: str = "access") -> Login:
    return Login(
        credentials=Credentials(access_token=token, refresh_token="refresh"),
        user=User(id=1, display_name="Anna", email="anna@example.com", customer_code=customer_code),
    )


LOGIN = make_login()
PENDING = BankIdProgress(reference="ref", auto_start_token="ast", qr_code="PHN2Zz4=", login=None)

pytestmark = pytest.mark.usefixtures("mock_setup_entry")


@pytest.fixture(autouse=True)
def no_bankid_poll_delay() -> Generator[None]:
    with patch("custom_components.umea_energi.config_flow.BANKID_POLL_INTERVAL", 0):
        yield


@pytest.fixture(autouse=True)
def cancel_bankid() -> Generator[AsyncMock]:
    with patch.object(UmeaEnergiClient, "cancel_bankid") as cancel:
        yield cancel


async def start_flow(
    hass: HomeAssistant, method: AuthMethod, account_type: AccountType = AccountType.PRIVATE
) -> ConfigFlowResult:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_TYPE: account_type, CONF_AUTH_METHOD: method}
    )


async def email_login(hass: HomeAssistant, result: ConfigFlowResult) -> ConfigFlowResult:
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "anna@example.com", CONF_PASSWORD: "secret"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACTIVATION_KEY: "1234"}
    )


async def test_email_flow(hass: HomeAssistant) -> None:
    with (
        patch.object(UmeaEnergiClient, "start_email_login") as start,
        patch.object(
            UmeaEnergiClient,
            "complete_email_login",
            side_effect=[ApiError("Wrong code", 400), LOGIN],
        ) as complete,
    ):
        result = await start_flow(hass, AuthMethod.EMAIL)
        assert result["step_id"] == "email"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: " anna@example.com ", CONF_PASSWORD: "secret"}
        )
        assert result["step_id"] == "email_code"
        start.assert_awaited_once_with("anna@example.com", "secret", CustomerType.PRIVATE)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACTIVATION_KEY: "0000"}
        )
        assert result["errors"] == {"base": "invalid_code"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACTIVATION_KEY: "1234"}
        )
        complete.assert_awaited_with("anna@example.com", "1234", CustomerType.PRIVATE)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == CUSTOMER_CODE
    assert result["data"] == {
        CONF_ACCOUNT_TYPE: AccountType.PRIVATE,
        CONF_AUTH_METHOD: AuthMethod.EMAIL,
        CONF_CUSTOMER_CODE: CUSTOMER_CODE,
        CONF_ACCESS_TOKEN: "access",
        CONF_REFRESH_TOKEN: "refresh",
        CONF_REFRESH_TOKEN_EXPIRES_AT: None,
        CONF_USERNAME: "anna@example.com",
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InvalidCredentialsError("Incorrect email or password", 404), "invalid_auth"),
        (ApiError("Request failed"), "cannot_connect"),
    ],
)
async def test_email_login_errors(hass: HomeAssistant, error: ApiError, expected: str) -> None:
    with patch.object(UmeaEnergiClient, "start_email_login", side_effect=error):
        result = await start_flow(hass, AuthMethod.EMAIL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "anna@example.com", CONF_PASSWORD: "wrong"}
        )

    assert result["step_id"] == "email"
    assert result["errors"] == {"base": expected}


async def test_bankid_flow(hass: HomeAssistant, cancel_bankid: AsyncMock) -> None:
    completed = BankIdProgress(reference="ref", auto_start_token=None, qr_code=None, login=LOGIN)
    with (
        patch.object(UmeaEnergiClient, "start_bankid", return_value=PENDING) as start,
        patch.object(UmeaEnergiClient, "poll_bankid", side_effect=[PENDING, completed]) as poll,
    ):
        result = await start_flow(hass, AuthMethod.BANKID, AccountType.BUSINESS)
        start.assert_awaited_once_with(CustomerType.COMPANY)
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        assert "autostarttoken=ast" in placeholders["auto_start_url"]
        assert placeholders["qr_code"] == "![BankID QR code](data:image/svg+xml;base64,PHN2Zz4=)"

        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    poll.assert_awaited_with("ref", CustomerType.COMPANY)
    assert poll.await_count == 2
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_METHOD] == AuthMethod.BANKID
    assert result["data"][CONF_ACCOUNT_TYPE] == AccountType.BUSINESS
    assert result["data"][CONF_ACCESS_TOKEN] == "access"
    cancel_bankid.assert_not_awaited()


async def test_bankid_failed_can_retry(hass: HomeAssistant) -> None:
    with (
        patch.object(UmeaEnergiClient, "start_bankid", return_value=PENDING) as start,
        patch.object(UmeaEnergiClient, "poll_bankid", side_effect=ApiError("Cancelled", 400)),
    ):
        result = await start_flow(hass, AuthMethod.BANKID)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["step_id"] == "bankid_failed"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert start.await_count == 2
        await hass.async_block_till_done()


async def test_abandoned_bankid_login_is_cancelled(
    hass: HomeAssistant, cancel_bankid: AsyncMock
) -> None:
    never_done = AsyncMock(side_effect=lambda *args: hass.loop.create_future())
    with (
        patch.object(UmeaEnergiClient, "start_bankid", return_value=PENDING),
        patch.object(UmeaEnergiClient, "poll_bankid", never_done),
    ):
        result = await start_flow(hass, AuthMethod.BANKID)
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()

    cancel_bankid.assert_awaited_once_with("ref", CustomerType.PRIVATE)


async def test_login_without_customer(hass: HomeAssistant) -> None:
    with (
        patch.object(UmeaEnergiClient, "start_email_login"),
        patch.object(UmeaEnergiClient, "complete_email_login", return_value=make_login(None)),
    ):
        result = await email_login(hass, await start_flow(hass, AuthMethod.EMAIL))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_customers"


async def test_already_configured(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    with (
        patch.object(UmeaEnergiClient, "start_email_login"),
        patch.object(UmeaEnergiClient, "complete_email_login", return_value=LOGIN),
    ):
        result = await email_login(hass, await start_flow(hass, AuthMethod.EMAIL))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("customer_code", "reason"),
    [(CUSTOMER_CODE, "reauth_successful"), ("999", "wrong_account")],
)
async def test_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    customer_code: str,
    reason: str,
) -> None:
    mock_config_entry.add_to_hass(hass)
    old_access_token = mock_config_entry.data[CONF_ACCESS_TOKEN]
    with (
        patch.object(UmeaEnergiClient, "start_email_login"),
        patch.object(
            UmeaEnergiClient,
            "complete_email_login",
            return_value=make_login(customer_code, token="new-access"),
        ),
    ):
        result = await mock_config_entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCOUNT_TYPE: AccountType.PRIVATE, CONF_AUTH_METHOD: AuthMethod.EMAIL},
        )
        result = await email_login(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    expected = "new-access" if reason == "reauth_successful" else old_access_token
    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == expected


async def test_options_flow(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPDATE_INTERVAL: 30}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_UPDATE_INTERVAL: 30}
