"""Tests for the API session wrapper."""

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from custom_components.umea_energi.api import Tokens, UmeaEnergiApi, UmeaEnergiAuthError
from custom_components.umea_energi.const import BASE_URL, AccountType, Utility

from .conftest import make_jwt

type Handler = Callable[[httpx.Request], httpx.Response]

LOGIN_SESSION = {
    "completed": True,
    "token": {"accessJwtToken": "access-1"},
    "user": {
        "firstName": "Anna",
        "lastName": "Svensson",
        "defaultCustomerCode": "111",
        "customer": {"code": "111"},
        "myCustomers": [{"customerCode": 222}],
        "refreshToken": {"token": "refresh-1"},
    },
}


def make_api(
    handler: Handler,
    tokens: Tokens | None = None,
    on_tokens_refreshed: Callable[[Tokens], None] | None = None,
) -> UmeaEnergiApi:
    http = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return UmeaEnergiApi(http, tokens, on_tokens_refreshed)


async def test_bankid_login() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v2/Session/bankid":
            return httpx.Response(
                200, json={"id": "ref", "autoStartToken": "ast", "qrCode": "qr-1"}
            )
        if request.url.path == "/api/v2/Session/bankid/ref/0":
            if len(requests) == 2:
                return httpx.Response(200, json={"completed": False, "qrCode": "qr-2"})
            return httpx.Response(200, json=LOGIN_SESSION)
        return httpx.Response(404)

    api = make_api(handler)

    started = await api.async_start_bankid(AccountType.PRIVATE)
    pending = await api.async_poll_bankid(started.reference, AccountType.PRIVATE)
    completed = await api.async_poll_bankid(started.reference, AccountType.PRIVATE)

    assert (started.reference, started.auto_start_token, started.qr_code) == ("ref", "ast", "qr-1")
    assert json.loads(requests[0].content) == {"type": 0}
    assert pending.login is None
    assert pending.qr_code == "qr-2"
    assert completed.login is not None
    assert completed.login.tokens == Tokens("access-1", "refresh-1")
    assert completed.login.customer_codes == ["111", "222"]
    assert completed.login.name == "Anna Svensson"
    assert api.tokens == Tokens("access-1", "refresh-1")


async def test_business_login_type() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "ref", "completed": False})

    api = make_api(handler)
    await api.async_start_bankid(AccountType.BUSINESS)
    await api.async_poll_bankid("ref", AccountType.BUSINESS)

    assert json.loads(requests[0].content) == {"type": 1}
    assert requests[1].url.path == "/api/v2/Session/bankid/ref/1"


async def test_bankid_poll_failure_is_auth_error() -> None:
    api = make_api(lambda request: httpx.Response(400))

    with pytest.raises(UmeaEnergiAuthError):
        await api.async_poll_bankid("ref", AccountType.PRIVATE)


@pytest.mark.parametrize(
    ("password", "path"),
    [("secret", "/api/v2/Session/emailStep1"), (None, "/api/v2/Session/emailWithKey")],
)
async def test_request_email_code(password: str | None, path: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    await make_api(handler).async_request_email_code(
        "anna@example.com", password, AccountType.PRIVATE
    )

    assert requests[0].url.path == path
    body = json.loads(requests[0].content)
    assert body["username"] == "anna@example.com"
    assert body.get("password") == password


async def test_email_code_login() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=LOGIN_SESSION)

    login = await make_api(handler).async_login_email_code(
        "anna@example.com", "1234", AccountType.PRIVATE
    )

    assert requests[0].url.path == "/api/v2/Session/emailStep2"
    assert json.loads(requests[0].content) == {
        "username": "anna@example.com",
        "activationKey": "1234",
        "type": 0,
    }
    assert login.tokens == Tokens("access-1", "refresh-1")


async def test_rejected_email_code() -> None:
    api = make_api(lambda request: httpx.Response(400))

    with pytest.raises(UmeaEnergiAuthError):
        await api.async_login_email_code("anna@example.com", "0000", AccountType.PRIVATE)


async def test_consumption_converts_local_times() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "consumption": [
                    {"period": "2026-10-23T01:00:00", "value": 2.5},
                    {"period": "2026-10-23T00:00:00", "value": 1.5},
                ]
            },
        )

    api = make_api(handler, Tokens(make_jwt(), "refresh-1"))
    readings = await api.async_get_consumption(
        Utility.DISTRICT_HEATING,
        "111",
        datetime(2026, 10, 22, 22, tzinfo=UTC),
        datetime(2026, 10, 23, 22, tzinfo=UTC),
    )

    request = requests[0]
    assert request.url.path == "/api/v2/Customer/111/heatingConsumption"
    assert request.url.params["From"] == "2026-10-23T00:00:00"
    assert request.url.params["To"] == "2026-10-24T00:00:00"
    assert request.url.params["Resolution"] == "HOUR"
    assert request.headers["Authorization"].startswith("Bearer ")
    assert [(r.start, r.value) for r in readings] == [
        (datetime(2026, 10, 22, 22, tzinfo=UTC), 1.5),
        (datetime(2026, 10, 22, 23, tzinfo=UTC), 2.5),
    ]


async def test_refreshes_token_when_rejected() -> None:
    requests: list[httpx.Request] = []
    refreshed: list[Tokens] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v2/Session/refresh-token":
            return httpx.Response(
                200,
                json={
                    "token": {"accessJwtToken": "access-2"},
                    "user": {"refreshToken": {"token": "refresh-2"}},
                },
            )
        if request.headers["Authorization"] == "Bearer access-2":
            return httpx.Response(200, json={"consumption": []})
        return httpx.Response(401)

    api = make_api(handler, Tokens("access-1", "refresh-1"), refreshed.append)
    now = datetime.now(UTC)
    await api.async_get_consumption(Utility.ELECTRICITY, "111", now, now)

    assert [r.url.path for r in requests] == [
        "/api/v2/Customer/111/electricityConsumption",
        "/api/v2/Session/refresh-token",
        "/api/v2/Customer/111/electricityConsumption",
    ]
    assert requests[1].headers["Cookie"] == "refreshToken=refresh-1"
    assert refreshed == [Tokens("access-2", "refresh-2")]


async def test_refreshes_expiring_token_before_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v2/Session/refresh-token":
            # Bare token body; the new refresh token comes as a cookie.
            return httpx.Response(
                200, text="access-2", headers={"Set-Cookie": "refreshToken=refresh-2; Path=/"}
            )
        return httpx.Response(200, json={"consumption": []})

    api = make_api(handler, Tokens(make_jwt(expires_in=10), "refresh-1"))
    now = datetime.now(UTC)
    await api.async_get_consumption(Utility.ELECTRICITY, "111", now, now)

    assert requests[0].url.path == "/api/v2/Session/refresh-token"
    assert requests[1].headers["Authorization"] == "Bearer access-2"
    assert api.tokens == Tokens("access-2", "refresh-2")


async def test_rejected_without_refresh_token() -> None:
    api = make_api(lambda request: httpx.Response(401), Tokens("access-1", None))
    now = datetime.now(UTC)

    with pytest.raises(UmeaEnergiAuthError):
        await api.async_get_consumption(Utility.ELECTRICITY, "111", now, now)
