"""Tests for credentials, meters and the consumption session."""

from datetime import UTC, datetime

import httpx
import pytest
from umea_energi_api_client import Credentials, MeterPoint, TokenExpiredError, UmeaEnergiClient
from umea_energi_api_client.testing import FakeApi

from custom_components.umea_energi.api import (
    Meter,
    Reading,
    UmeaEnergiSession,
    credentials_from_data,
    credentials_to_data,
    meters_from_points,
    parse_readings,
)
from custom_components.umea_energi.const import Utility

from .conftest import make_jwt

CONSUMPTION_PATH = "/api/v1/Customer/128/electricityConsumption/ELECTRICITY_GRID"
REFRESH_PATH = "/api/v1/Session/refresh-token"


def meter_point(contract_type: str, number: str = "735") -> MeterPoint:
    return MeterPoint(
        contract_type=contract_type,
        meter_point_no=number,
        customer_code="128",
        address="Street 1",
        contract_id=None,
    )


GRID_METER = Meter(Utility.ELECTRICITY, meter_point("ELECTRICITY_GRID"))


def make_session(
    fake: FakeApi, credentials: Credentials, changed: list[Credentials]
) -> UmeaEnergiSession:
    client = UmeaEnergiClient("https://api.test", httpx_args={"transport": fake.transport})
    return UmeaEnergiSession(client, credentials, changed.append)


def test_credentials_round_trip() -> None:
    credentials = Credentials(
        access_token="access",
        refresh_token="refresh",
        refresh_token_expires_at=datetime(2027, 10, 25, tzinfo=UTC),
    )

    assert credentials_from_data(credentials_to_data(credentials)) == credentials


def test_meters_prefer_grid_contracts() -> None:
    points = [
        meter_point("ELECTRICITY_GRID", "735"),
        meter_point("ELECTRICITY", "735"),  # same meter as the grid contract
        meter_point("ELECTRICITY", "736"),  # supply only; another company owns the grid
        meter_point("DISTRICT_HEATING", "900"),
        meter_point("BROADBAND", "1"),
    ]

    meters = meters_from_points(points)

    assert [(m.utility, m.meter_point.contract_type, m.key) for m in meters] == [
        (Utility.ELECTRICITY, "ELECTRICITY_GRID", "electricity_735"),
        (Utility.ELECTRICITY, "ELECTRICITY", "electricity_736"),
        (Utility.DISTRICT_HEATING, "DISTRICT_HEATING", "district_heating_900"),
    ]
    assert meters[0].location == "Street 1"


def test_parse_readings() -> None:
    readings = parse_readings(
        {
            "consumption": [
                {"period": "2026-10-23T01:00:00", "value": 2},
                {"period": "2026-10-23T00:00:00", "value": 1.5},  # local time, out of order
                {"period": "2026-10-23T02:00:00+00:00", "value": 3.0},
                {"period": "not a date", "value": 1.0},
                {"period": "2026-10-23T03:00:00", "value": None},
                {"period": "2026-10-23T04:00:00", "value": True},
                "junk",
            ]
        }
    )

    assert readings == [
        Reading(datetime(2026, 10, 22, 22, tzinfo=UTC), 1.5),
        Reading(datetime(2026, 10, 22, 23, tzinfo=UTC), 2.0),
        Reading(datetime(2026, 10, 23, 2, tzinfo=UTC), 3.0),
    ]
    assert parse_readings([]) == []
    assert parse_readings({"consumption": None}) == []


async def test_hourly_consumption_asks_for_whole_days() -> None:
    fake = FakeApi()
    fake.add(
        "GET",
        CONSUMPTION_PATH,
        httpx.Response(
            200,
            json={
                "consumption": [
                    {"period": "2026-10-22T23:00:00", "value": 9.0},  # before start
                    {"period": "2026-10-23T00:00:00", "value": 1.0},
                    {"period": "2026-10-23T01:00:00", "value": 2.0},
                ]
            },
        ),
    )
    session = make_session(fake, Credentials(access_token=make_jwt()), [])

    readings = await session.async_hourly_consumption(
        GRID_METER,
        datetime(2026, 10, 22, 22, tzinfo=UTC),  # midnight 23 October, local time
        datetime(2026, 10, 23, 12, tzinfo=UTC),
    )

    [request] = fake.requests
    assert request.url.params["From"] == "2026-10-23T00:00:00"
    assert request.url.params["To"] == "2026-10-24T00:00:00"
    assert request.url.params["Resolution"] == "HOUR"
    assert request.url.params["MeterPointNo"] == "735"
    assert [r.value for r in readings] == [1.0, 2.0]


async def test_refreshes_expiring_token_and_reports_it() -> None:
    fake = FakeApi()
    new_token = make_jwt()
    fake.add(
        "POST",
        REFRESH_PATH,
        httpx.Response(
            200,
            json={"token": {"access_token": new_token}},
            headers={"set-cookie": "refreshToken=refresh-2; path=/; secure; httponly"},
        ),
    )
    fake.add("GET", CONSUMPTION_PATH, httpx.Response(200, json={"consumption": []}))
    changed: list[Credentials] = []
    old = Credentials(access_token=make_jwt(expires_in=10), refresh_token="refresh-1")
    session = make_session(fake, old, changed)

    now = datetime.now(UTC)
    await session.async_hourly_consumption(GRID_METER, now, now)

    refresh, consumption = fake.requests
    assert refresh.headers["cookie"] == "refreshToken=refresh-1"
    assert consumption.headers["authorization"] == f"Bearer {new_token}"
    assert [(c.access_token, c.refresh_token) for c in changed] == [(new_token, "refresh-2")]


async def test_retries_once_after_a_rejected_token() -> None:
    fake = FakeApi()
    new_token = make_jwt()
    fake.add("POST", REFRESH_PATH, httpx.Response(200, json={"token": {"access_token": new_token}}))
    fake.add(
        "GET",
        CONSUMPTION_PATH,
        httpx.Response(401),
        httpx.Response(200, json={"consumption": []}),
    )
    changed: list[Credentials] = []
    session = make_session(
        fake, Credentials(access_token=make_jwt(), refresh_token="refresh-1"), changed
    )

    now = datetime.now(UTC)
    await session.async_hourly_consumption(GRID_METER, now, now)

    assert [r.url.path for r in fake.requests] == [CONSUMPTION_PATH, REFRESH_PATH, CONSUMPTION_PATH]
    assert changed[0].access_token == new_token
    assert changed[0].refresh_token == "refresh-1"  # not rotated


@pytest.mark.parametrize(
    "credentials",
    [
        # Expired, and the refresh token is rejected.
        Credentials(access_token=make_jwt(expires_in=-10), refresh_token="revoked"),
        # Expired, and nothing to refresh with.
        Credentials(access_token=make_jwt(expires_in=-10)),
    ],
)
async def test_session_that_cannot_be_renewed(credentials: Credentials) -> None:
    fake = FakeApi()
    fake.add("POST", REFRESH_PATH, httpx.Response(401))
    session = make_session(fake, credentials, [])

    now = datetime.now(UTC)
    with pytest.raises(TokenExpiredError):
        await session.async_hourly_consumption(GRID_METER, now, now)
