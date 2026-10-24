"""Session handling and data access on top of umea-energi-api-client."""

import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from operator import attrgetter
from typing import Any

import httpx
from homeassistant.helpers.httpx_client import SERVER_SOFTWARE
from homeassistant.util.ssl import client_context
from umea_energi_api_client import AuthenticatedClient, Client
from umea_energi_api_client.generated.api.customer import (
    get_api_vversion_customer_customer_code_electricity_consumption as electricity_consumption,
)
from umea_energi_api_client.generated.api.customer import (
    get_api_vversion_customer_customer_code_heating_consumption as heating_consumption,
)
from umea_energi_api_client.generated.api.session import (
    get_api_vversion_session_bankid_reference_type as get_bankid_session,
)
from umea_energi_api_client.generated.api.session import (
    post_api_vversion_session_bankid as post_bankid,
)
from umea_energi_api_client.generated.api.session import (
    post_api_vversion_session_email_step_1 as post_email_step_1,
)
from umea_energi_api_client.generated.api.session import (
    post_api_vversion_session_email_step_2 as post_email_step_2,
)
from umea_energi_api_client.generated.api.session import (
    post_api_vversion_session_email_with_key as post_email_with_key,
)
from umea_energi_api_client.generated.api.session import (
    post_api_vversion_session_refresh_token as post_refresh_token,
)
from umea_energi_api_client.generated.models import (
    UmeaEnergiApiMiscsLoginManagerState,
    UmeaEnergiApiModelsConsumptionResponse,
    UmeaEnergiApiModelsLoginActivationKeyCredentials,
    UmeaEnergiApiModelsLoginBankidCredentials,
    UmeaEnergiApiModelsLoginEmailCredentials,
    UmeaEnergiApiModelsLoginLoginSession,
    UmeaEnergiApiModelsLoginUserSession,
)
from umea_energi_api_client.generated.types import Response, Unset

from .const import API_TIMEZONE, API_VERSION, BASE_URL, AccountType, Utility

# The login `type` (LoginManagerState): 0 for private customers, 1 for business customers.
_LOGIN_TYPES = {
    AccountType.PRIVATE: UmeaEnergiApiMiscsLoginManagerState.VALUE_0,
    AccountType.BUSINESS: UmeaEnergiApiMiscsLoginManagerState.VALUE_1,
}
# The refresh-token endpoint reads the refresh token from this cookie.
REFRESH_TOKEN_COOKIE = "refreshToken"
# Refresh the access token when it expires within this many seconds.
TOKEN_REFRESH_MARGIN = 300

_CONSUMPTION_ENDPOINTS = {
    Utility.ELECTRICITY: electricity_consumption.asyncio_detailed,
    Utility.DISTRICT_HEATING: heating_consumption.asyncio_detailed,
}


def create_http_client() -> httpx.AsyncClient:
    """An httpx client for one API session; the caller must close it.

    Home Assistant's shared clients can't be used: the session needs its own base URL,
    bearer token and cookie jar.
    """
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"User-Agent": SERVER_SOFTWARE},
        verify=client_context(),
        timeout=30,
    )


class UmeaEnergiError(Exception):
    """The Umeå Energi API could not be reached or returned something unexpected."""


class UmeaEnergiAuthError(UmeaEnergiError):
    """Credentials were rejected or the session can no longer be refreshed."""


@dataclass(frozen=True, slots=True)
class Tokens:
    """Access token, plus the refresh token used to renew it."""

    access_token: str
    refresh_token: str | None


@dataclass(frozen=True, slots=True)
class Login:
    """Result of a completed login."""

    tokens: Tokens
    customer_codes: list[str]
    name: str | None


@dataclass(frozen=True, slots=True)
class BankIdSession:
    """State of a pending BankID login; `login` is set once the user has signed."""

    reference: str
    auto_start_token: str | None
    qr_code: str | None
    login: Login | None


@dataclass(frozen=True, slots=True)
class Reading:
    """Consumption during the hour starting at `start` (UTC), in kWh."""

    start: datetime
    value: float


class UmeaEnergiApi:
    """Umeå Energi API session.

    The httpx client must have `base_url` set; it carries the bearer token and cookies, so use
    one client per session.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: Tokens | None = None,
        on_tokens_refreshed: Callable[[Tokens], None] | None = None,
    ) -> None:
        self._http = http
        self._client = Client(base_url=str(http.base_url)).set_async_httpx_client(http)
        self._authed_client: AuthenticatedClient | None = None
        self._on_tokens_refreshed = on_tokens_refreshed
        self._tokens: Tokens | None = None
        if tokens is not None:
            self._set_tokens(tokens)

    @property
    def tokens(self) -> Tokens | None:
        """Current tokens, if logged in."""
        return self._tokens

    async def async_start_bankid(self, account_type: AccountType) -> BankIdSession:
        """Start a BankID login."""
        body = UmeaEnergiApiModelsLoginBankidCredentials(type_=_LOGIN_TYPES[account_type])
        response = await self._call(
            lambda: post_bankid.asyncio_detailed(
                version=API_VERSION, client=self._client, body=body
            )
        )
        session = _parsed(response, UmeaEnergiApiModelsLoginLoginSession)
        if not (reference := _value(session.id)):
            raise UmeaEnergiError("BankID response did not include a reference")
        return self._bankid_session(reference, session)

    async def async_poll_bankid(self, reference: str, account_type: AccountType) -> BankIdSession:
        """Check a pending BankID login; returns a fresh QR code while it is pending."""
        response = await self._call(
            lambda: get_bankid_session.asyncio_detailed(
                version=API_VERSION,
                reference=reference,
                type_=_LOGIN_TYPES[account_type],
                client=self._client,
            )
        )
        if response.status_code == 400:
            raise UmeaEnergiAuthError("BankID login failed, expired or was cancelled")
        session = _parsed(response, UmeaEnergiApiModelsLoginLoginSession)
        return self._bankid_session(reference, session)

    async def async_request_email_code(
        self, username: str, password: str | None, account_type: AccountType
    ) -> None:
        """Ask Umeå Energi to email a one-time login code.

        With a password this is the two-step password login, without one the passwordless login.
        """
        body = UmeaEnergiApiModelsLoginEmailCredentials(
            username=username, password=password, type_=_LOGIN_TYPES[account_type]
        )
        if password:
            response = await self._call(
                lambda: post_email_step_1.asyncio_detailed(
                    version=API_VERSION, client=self._client, body=body
                )
            )
        else:
            response = await self._call(
                lambda: post_email_with_key.asyncio_detailed(
                    version=API_VERSION, client=self._client, body=body
                )
            )
        _raise_for_login_status(response)

    async def async_login_email_code(
        self, username: str, activation_key: str, account_type: AccountType
    ) -> Login:
        """Complete an email login with the code from the email."""
        body = UmeaEnergiApiModelsLoginActivationKeyCredentials(
            username=username,
            activation_key=activation_key,
            type_=_LOGIN_TYPES[account_type],
        )
        response = await self._call(
            lambda: post_email_step_2.asyncio_detailed(
                version=API_VERSION, client=self._client, body=body
            )
        )
        _raise_for_login_status(response)
        login = self._login(_parsed(response, UmeaEnergiApiModelsLoginLoginSession))
        self._set_tokens(login.tokens)
        return login

    async def async_refresh_tokens(self) -> Tokens:
        """Exchange the refresh token for a new access token."""
        if self._tokens is None or not self._tokens.refresh_token:
            raise UmeaEnergiAuthError("No refresh token available")
        self._http.cookies.set(
            REFRESH_TOKEN_COOKIE, self._tokens.refresh_token, domain=self._http.base_url.host
        )
        response = await self._call(
            lambda: post_refresh_token.asyncio_detailed(version=API_VERSION, client=self._client)
        )
        if response.status_code != 200:
            raise UmeaEnergiAuthError(f"Token refresh failed with HTTP {response.status_code}")
        # The response body is undocumented; accept a session object or a bare token.
        access_token, refresh_token = _tokens_from_refresh_payload(response.content)
        tokens = Tokens(
            access_token=access_token,
            refresh_token=refresh_token or self._refresh_cookie() or self._tokens.refresh_token,
        )
        self._set_tokens(tokens)
        if self._on_tokens_refreshed is not None:
            self._on_tokens_refreshed(tokens)
        return tokens

    async def async_get_consumption(
        self, utility: Utility, customer_code: str, start: datetime, end: datetime
    ) -> list[Reading]:
        """Hourly consumption between two aware datetimes, oldest first."""
        endpoint = _CONSUMPTION_ENDPOINTS[utility]
        response = await self._authed(
            lambda: endpoint(
                version=API_VERSION,
                customer_code=customer_code,
                client=self._authenticated_client(),
                from_=_to_api_time(start),
                to=_to_api_time(end),
                resolution="HOUR",
            )
        )
        result = _parsed(response, UmeaEnergiApiModelsConsumptionResponse)
        readings = [
            Reading(start=_from_api_time(period), value=value)
            for point in _value(result.consumption) or []
            if (period := _value(point.period)) is not None
            and (value := _value(point.value)) is not None
        ]
        return sorted(readings, key=attrgetter("start"))

    def _set_tokens(self, tokens: Tokens) -> None:
        self._tokens = tokens
        # A client set with `set_async_httpx_client` doesn't add its token, so the shared httpx
        # client carries the header.
        self._http.headers["Authorization"] = f"Bearer {tokens.access_token}"
        self._authed_client = AuthenticatedClient(
            base_url=str(self._http.base_url), token=tokens.access_token
        ).set_async_httpx_client(self._http)

    def _authenticated_client(self) -> AuthenticatedClient:
        if self._authed_client is None:
            raise UmeaEnergiAuthError("Not logged in")
        return self._authed_client

    def _refresh_cookie(self) -> str | None:
        # Read the jar directly: `cookies.get` raises when several domains set the same name.
        values = [
            cookie.value for cookie in self._http.cookies.jar if cookie.name == REFRESH_TOKEN_COOKIE
        ]
        return values[-1] if values else None

    def _login(
        self, session: UmeaEnergiApiModelsLoginLoginSession | UmeaEnergiApiModelsLoginUserSession
    ) -> Login:
        login = _login_from_session(session)
        if login.tokens.refresh_token is None:
            tokens = Tokens(login.tokens.access_token, self._refresh_cookie())
            login = Login(tokens=tokens, customer_codes=login.customer_codes, name=login.name)
        return login

    def _bankid_session(
        self, reference: str, session: UmeaEnergiApiModelsLoginLoginSession
    ) -> BankIdSession:
        login = None
        if session.completed is True:
            login = self._login(session)
            self._set_tokens(login.tokens)
        return BankIdSession(
            reference=reference,
            auto_start_token=_value(session.auto_start_token),
            qr_code=_value(session.qr_code),
            login=login,
        )

    async def _authed[T](self, request: Callable[[], Awaitable[Response[T]]]) -> Response[T]:
        if self._tokens is None:
            raise UmeaEnergiAuthError("Not logged in")
        can_refresh = self._tokens.refresh_token is not None
        if can_refresh and _expires_soon(self._tokens.access_token):
            await self.async_refresh_tokens()
        response = await self._call(request)
        if response.status_code == 401 and can_refresh:
            await self.async_refresh_tokens()
            response = await self._call(request)
        if response.status_code in (401, 403):
            raise UmeaEnergiAuthError(f"Request rejected with HTTP {response.status_code}")
        return response

    async def _call[T](self, request: Callable[[], Awaitable[Response[T]]]) -> Response[T]:
        try:
            return await request()
        except httpx.HTTPError as err:
            raise UmeaEnergiError(f"Error communicating with Umeå Energi: {err}") from err
        except (KeyError, TypeError, ValueError) as err:
            raise UmeaEnergiError(f"Unexpected response from Umeå Energi: {err}") from err


def _value[T](value: T | Unset | None) -> T | None:
    return None if isinstance(value, Unset) else value


def _parsed[T](response: Response[Any], model: type[T]) -> T:
    if response.status_code in (401, 403):
        raise UmeaEnergiAuthError(f"Request rejected with HTTP {response.status_code}")
    if not isinstance(response.parsed, model):
        raise UmeaEnergiError(f"Unexpected response from Umeå Energi (HTTP {response.status_code})")
    return response.parsed


def _raise_for_login_status(response: Response[Any]) -> None:
    if response.status_code in (400, 401, 403, 404):
        raise UmeaEnergiAuthError(f"Login rejected with HTTP {response.status_code}")
    if response.status_code != 200:
        raise UmeaEnergiError(f"Login failed with HTTP {response.status_code}")


def _login_from_session(
    session: UmeaEnergiApiModelsLoginLoginSession | UmeaEnergiApiModelsLoginUserSession,
) -> Login:
    token = _value(session.token)
    access_token = _value(token.access_jwt_token) if token is not None else None
    if not access_token:
        raise UmeaEnergiAuthError("Login response did not include an access token")

    refresh_token: str | None = None
    customer_codes: list[str] = []
    name: str | None = None
    if (user := _value(session.user)) is not None:
        if (refresh := _value(user.refresh_token)) is not None:
            refresh_token = _value(refresh.token)
        candidates = [_value(user.default_customer_code)]
        if (customer := _value(user.customer)) is not None:
            candidates.append(_value(customer.code))
        candidates.extend(
            str(code)
            for delegate in _value(user.my_customers) or []
            if (code := _value(delegate.customer_code)) is not None
        )
        customer_codes = list(dict.fromkeys(code for code in candidates if code))
        name = " ".join(filter(None, [_value(user.first_name), _value(user.last_name)])) or None

    return Login(
        tokens=Tokens(access_token=access_token, refresh_token=refresh_token),
        customer_codes=customer_codes,
        name=name,
    )


def _tokens_from_refresh_payload(content: bytes) -> tuple[str, str | None]:
    try:
        payload = json.loads(content)
    except ValueError:
        payload = content.decode(errors="replace").strip()
    if isinstance(payload, str) and payload:
        return payload, None
    if isinstance(payload, dict):
        if isinstance(token := payload.get("accessJwtToken"), str) and token:
            return token, None
        login = _login_from_session(UmeaEnergiApiModelsLoginUserSession.from_dict(payload))
        return login.tokens.access_token, login.tokens.refresh_token
    raise UmeaEnergiAuthError("Token refresh response did not include an access token")


def _expires_soon(jwt: str) -> bool:
    """Whether the JWT expires within the refresh margin; False if it can't be decoded."""
    try:
        payload = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        expires = float(claims["exp"])
    except IndexError, KeyError, TypeError, ValueError:
        return False
    return expires - time.time() < TOKEN_REFRESH_MARGIN


def _to_api_time(value: datetime) -> datetime:
    return value.astimezone(API_TIMEZONE).replace(tzinfo=None)


def _from_api_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=API_TIMEZONE)
    return value.astimezone(UTC)
