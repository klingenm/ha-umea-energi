"""Tests for the Umeå Energi config flow."""

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.umea_energi.api import (
    BankIdSession,
    Login,
    Tokens,
    UmeaEnergiApi,
    UmeaEnergiAuthError,
)
from custom_components.umea_energi.const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_TYPE,
    CONF_ACTIVATION_KEY,
    CONF_AUTH_METHOD,
    CONF_CUSTOMER_CODE,
    CONF_REFRESH_TOKEN,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
    AccountType,
    AuthMethod,
)

from .conftest import CUSTOMER_CODE

LOGIN = Login(tokens=Tokens("access", "refresh"), customer_codes=[CUSTOMER_CODE], name="Anna")
PENDING = BankIdSession(reference="ref", auto_start_token="ast", qr_code="qr", login=None)

pytestmark = pytest.mark.usefixtures("mock_setup_entry")


@pytest.fixture(autouse=True)
def no_bankid_poll_delay() -> Generator[None]:
    with patch("custom_components.umea_energi.config_flow.BANKID_POLL_INTERVAL", 0):
        yield


async def start_flow(
    hass: HomeAssistant, method: AuthMethod, account_type: AccountType = AccountType.PRIVATE
) -> ConfigFlowResult:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT_TYPE: account_type, CONF_AUTH_METHOD: method}
    )


async def test_email_flow(hass: HomeAssistant) -> None:
    with (
        patch.object(UmeaEnergiApi, "async_request_email_code") as request_code,
        patch.object(
            UmeaEnergiApi,
            "async_login_email_code",
            side_effect=[UmeaEnergiAuthError("bad code"), LOGIN],
        ) as login,
    ):
        result = await start_flow(hass, AuthMethod.EMAIL)
        assert result["step_id"] == "email"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "anna@example.com", CONF_PASSWORD: "secret"}
        )
        assert result["step_id"] == "email_code"
        request_code.assert_awaited_once_with("anna@example.com", "secret", AccountType.PRIVATE)

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACTIVATION_KEY: "0000"}
        )
        assert result["errors"] == {"base": "invalid_code"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACTIVATION_KEY: " 1234 "}
        )
        login.assert_awaited_with("anna@example.com", "1234", AccountType.PRIVATE)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == CUSTOMER_CODE
    assert result["data"] == {
        CONF_ACCOUNT_TYPE: AccountType.PRIVATE,
        CONF_AUTH_METHOD: AuthMethod.EMAIL,
        CONF_CUSTOMER_CODE: CUSTOMER_CODE,
        CONF_ACCESS_TOKEN: "access",
        CONF_REFRESH_TOKEN: "refresh",
        CONF_USERNAME: "anna@example.com",
    }


async def test_email_flow_without_password(hass: HomeAssistant) -> None:
    with patch.object(UmeaEnergiApi, "async_request_email_code") as request_code:
        result = await start_flow(hass, AuthMethod.EMAIL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "anna@example.com"}
        )

    assert result["step_id"] == "email_code"
    request_code.assert_awaited_once_with("anna@example.com", None, AccountType.PRIVATE)


async def test_email_rejected(hass: HomeAssistant) -> None:
    with patch.object(
        UmeaEnergiApi, "async_request_email_code", side_effect=UmeaEnergiAuthError("no")
    ):
        result = await start_flow(hass, AuthMethod.EMAIL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "anna@example.com", CONF_PASSWORD: "wrong"}
        )

    assert result["step_id"] == "email"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_bankid_flow(hass: HomeAssistant) -> None:
    completed = BankIdSession(reference="ref", auto_start_token=None, qr_code=None, login=LOGIN)
    with (
        patch.object(UmeaEnergiApi, "async_start_bankid", return_value=PENDING) as start,
        patch.object(UmeaEnergiApi, "async_poll_bankid", side_effect=[PENDING, completed]) as poll,
    ):
        result = await start_flow(hass, AuthMethod.BANKID, AccountType.BUSINESS)
        start.assert_awaited_once_with(AccountType.BUSINESS)
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        assert "autostarttoken=ast" in placeholders["auto_start_url"]
        assert "data:image/png;base64," in placeholders["qr_code"]

        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    poll.assert_awaited_with("ref", AccountType.BUSINESS)
    assert poll.await_count == 2
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AUTH_METHOD] == AuthMethod.BANKID
    assert result["data"][CONF_ACCOUNT_TYPE] == AccountType.BUSINESS
    assert result["data"][CONF_ACCESS_TOKEN] == "access"


async def test_bankid_failed_can_retry(hass: HomeAssistant) -> None:
    with (
        patch.object(UmeaEnergiApi, "async_start_bankid", return_value=PENDING) as start,
        patch.object(UmeaEnergiApi, "async_poll_bankid", side_effect=UmeaEnergiAuthError("no")),
    ):
        result = await start_flow(hass, AuthMethod.BANKID)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["step_id"] == "bankid_failed"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert start.await_count == 2
        await hass.async_block_till_done()


async def test_choose_customer(hass: HomeAssistant) -> None:
    login = Login(tokens=LOGIN.tokens, customer_codes=["111", "222"], name=None)
    with (
        patch.object(UmeaEnergiApi, "async_request_email_code"),
        patch.object(UmeaEnergiApi, "async_login_email_code", return_value=login),
    ):
        result = await start_flow(hass, AuthMethod.EMAIL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "anna@example.com"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACTIVATION_KEY: "1234"}
        )
        assert result["step_id"] == "customer"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CUSTOMER_CODE: "222"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CUSTOMER_CODE] == "222"


async def test_already_configured(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    mock_config_entry.add_to_hass(hass)
    with (
        patch.object(UmeaEnergiApi, "async_request_email_code"),
        patch.object(UmeaEnergiApi, "async_login_email_code", return_value=LOGIN),
    ):
        result = await start_flow(hass, AuthMethod.EMAIL)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "anna@example.com"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACTIVATION_KEY: "1234"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    ("customer_codes", "reason"),
    [([CUSTOMER_CODE, "999"], "reauth_successful"), (["999"], "wrong_account")],
)
async def test_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    customer_codes: list[str],
    reason: str,
) -> None:
    mock_config_entry.add_to_hass(hass)
    old_access_token = mock_config_entry.data[CONF_ACCESS_TOKEN]
    login = Login(Tokens("new-access", "new-refresh"), customer_codes=customer_codes, name=None)
    with (
        patch.object(UmeaEnergiApi, "async_request_email_code"),
        patch.object(UmeaEnergiApi, "async_login_email_code", AsyncMock(return_value=login)),
    ):
        result = await mock_config_entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCOUNT_TYPE: AccountType.PRIVATE, CONF_AUTH_METHOD: AuthMethod.EMAIL},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: "anna@example.com"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACTIVATION_KEY: "1234"}
        )

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
