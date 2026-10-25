"""Config flow for Umeå Energi: sign in with BankID or email and an emailed one-time code."""

import asyncio
import time
from collections.abc import Mapping
from typing import Any, override

import probatio
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from umea_energi_api_client import (
    ApiError,
    BankIdProgress,
    InvalidCredentialsError,
    Login,
    UmeaEnergiError,
)

from .api import create_client, credentials_to_data
from .const import (
    CONF_ACCOUNT_TYPE,
    CONF_ACTIVATION_KEY,
    CONF_AUTH_METHOD,
    CONF_CUSTOMER_CODE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    AccountType,
    AuthMethod,
)

# Seconds between BankID status checks. BankID's QR code changes every second, and each check
# returns the current one.
BANKID_POLL_INTERVAL = 1.0
# Give up waiting for the user to sign after this many seconds.
BANKID_TIMEOUT = 300.0

STEP_USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_ACCOUNT_TYPE, default=AccountType.PRIVATE): SelectSelector(
            SelectSelectorConfig(options=list(AccountType), translation_key=CONF_ACCOUNT_TYPE)
        ),
        probatio.Required(CONF_AUTH_METHOD, default=AuthMethod.BANKID): SelectSelector(
            SelectSelectorConfig(options=list(AuthMethod), translation_key=CONF_AUTH_METHOD)
        ),
    }
)
STEP_EMAIL_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        probatio.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)
STEP_EMAIL_CODE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_ACTIVATION_KEY): TextSelector(
            TextSelectorConfig(autocomplete="one-time-code")
        ),
    }
)
OPTIONS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_UPDATE_INTERVAL,
                max=MAX_UPDATE_INTERVAL,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
)


class UmeaEnergiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Umeå Energi."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._client = create_client()
        self._account_type = AccountType.PRIVATE
        self._auth_method = AuthMethod.BANKID
        self._username: str | None = None
        self._bankid: BankIdProgress | None = None
        self._bankid_task: asyncio.Task[BankIdProgress] | None = None
        self._bankid_deadline = 0.0
        self._login: Login | None = None

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> UmeaEnergiOptionsFlow:
        """Get the options flow for this handler."""
        return UmeaEnergiOptionsFlow()

    @override
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user choose account type and how to sign in."""
        if user_input is not None:
            self._account_type = AccountType(user_input[CONF_ACCOUNT_TYPE])
            self._auth_method = AuthMethod(user_input[CONF_AUTH_METHOD])
            if self._auth_method is AuthMethod.BANKID:
                return await self.async_step_bankid()
            return await self.async_step_email()

        suggested: Mapping[str, Any] = {}
        if self.source == SOURCE_REAUTH:
            suggested = self._get_reauth_entry().data
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, suggested),
        )

    async def async_step_bankid(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in with BankID, polling until the user has signed in the BankID app."""
        if self._bankid is None:
            try:
                self._bankid = await self._client.start_bankid(self._account_type.customer_type)
            except UmeaEnergiError as err:
                LOGGER.error("Could not start BankID login: %s", err)
                return self.async_abort(reason="cannot_connect")
            self._bankid_deadline = time.monotonic() + BANKID_TIMEOUT

        if self._bankid_task is None:
            self._bankid_task = self.hass.async_create_task(
                self._async_poll_bankid(self._bankid.reference)
            )
        if not self._bankid_task.done():
            return self.async_show_progress(
                step_id="bankid",
                progress_action="bankid",
                progress_task=self._bankid_task,
                description_placeholders=_bankid_placeholders(self._bankid),
            )

        task, self._bankid_task = self._bankid_task, None
        try:
            self._bankid = task.result()
        except UmeaEnergiError as err:
            LOGGER.debug("BankID login failed: %s", err)
            self._bankid = None
            return self.async_show_progress_done(next_step_id="bankid_failed")
        if (login := self._bankid.login) is not None:
            self._login = login
            return self.async_show_progress_done(next_step_id="finish_login")
        if time.monotonic() > self._bankid_deadline:
            await self._async_cancel_bankid(self._bankid.reference)
            self._bankid = None
            return self.async_show_progress_done(next_step_id="bankid_failed")
        # Still pending: poll again, showing the current QR code.
        return await self.async_step_bankid()

    async def async_step_bankid_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Tell the user the BankID login did not complete, and offer to retry."""
        if user_input is None:
            return self.async_show_form(step_id="bankid_failed")
        return await self.async_step_bankid()

    async def async_step_email(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Check the email and password; Umeå Energi then emails a one-time code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            try:
                await self._client.start_email_login(
                    self._username, user_input[CONF_PASSWORD], self._account_type.customer_type
                )
            except InvalidCredentialsError:
                errors["base"] = "invalid_auth"
            except UmeaEnergiError as err:
                LOGGER.error("Could not start email login: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_email_code()

        suggested = {CONF_USERNAME: self._username}
        if self._username is None and self.source == SOURCE_REAUTH:
            suggested[CONF_USERNAME] = self._get_reauth_entry().data.get(CONF_USERNAME)
        return self.async_show_form(
            step_id="email",
            data_schema=self.add_suggested_values_to_schema(STEP_EMAIL_SCHEMA, suggested),
            errors=errors,
        )

    async def async_step_email_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Complete the email login with the code from the email."""
        assert self._username is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._login = await self._client.complete_email_login(
                    self._username,
                    user_input[CONF_ACTIVATION_KEY],
                    self._account_type.customer_type,
                )
            except ApiError as err:
                if err.status_code is not None and 400 <= err.status_code < 500:
                    errors["base"] = "invalid_code"
                else:
                    LOGGER.error("Could not complete email login: %s", err)
                    errors["base"] = "cannot_connect"
            except UmeaEnergiError as err:
                LOGGER.error("Could not complete email login: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_finish_login()

        return self.async_show_form(
            step_id="email_code",
            data_schema=STEP_EMAIL_CODE_SCHEMA,
            errors=errors,
            description_placeholders={"username": self._username},
        )

    async def async_step_finish_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry for the logged-in customer, or update it when reauthenticating."""
        assert self._login is not None
        user = self._login.user
        if user is None or not user.customer_code:
            return self.async_abort(reason="no_customers")
        customer_code = user.customer_code

        data: dict[str, Any] = {
            CONF_ACCOUNT_TYPE: self._account_type,
            CONF_AUTH_METHOD: self._auth_method,
            CONF_CUSTOMER_CODE: customer_code,
            **credentials_to_data(self._login.credentials),
        }
        if self._username is not None:
            data[CONF_USERNAME] = self._username

        await self.async_set_unique_id(customer_code)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"Umeå Energi {customer_code}", data=data)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle an expired session."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to sign in again."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    @callback
    @override
    def async_remove(self) -> None:
        """Stop polling, and cancel a BankID order the user never completed."""
        if self._bankid_task is not None:
            self._bankid_task.cancel()
        if self._bankid is not None and self._bankid.login is None:
            self.hass.async_create_task(self._async_cancel_bankid(self._bankid.reference))

    async def _async_poll_bankid(self, reference: str) -> BankIdProgress:
        await asyncio.sleep(BANKID_POLL_INTERVAL)
        return await self._client.poll_bankid(reference, self._account_type.customer_type)

    async def _async_cancel_bankid(self, reference: str) -> None:
        try:
            await self._client.cancel_bankid(reference, self._account_type.customer_type)
        except UmeaEnergiError as err:
            LOGGER.debug("Could not cancel BankID order: %s", err)


class UmeaEnergiOptionsFlow(OptionsFlowWithReload):
    """Options for Umeå Energi."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure the update interval."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )


def _bankid_placeholders(progress: BankIdProgress) -> dict[str, str]:
    return {
        "auto_start_url": (
            f"https://app.bankid.com/?autostarttoken={progress.auto_start_token or ''}"
            "&redirect=null"
        ),
        "qr_code": _qr_code_markdown(progress.qr_code),
    }


def _qr_code_markdown(qr_code: str | None) -> str:
    """Render the BankID QR code, a base64-encoded SVG, as a markdown image."""
    if not qr_code:
        return ""
    uri = qr_code if qr_code.startswith("data:image/") else f"data:image/svg+xml;base64,{qr_code}"
    return f"![BankID QR code]({uri})"
