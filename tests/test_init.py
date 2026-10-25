"""Tests for setting up the integration, its sensors and statistics."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)
from umea_energi_api_client import (
    ApiError,
    Credentials,
    MeterPoint,
    Resolution,
    TokenExpiredError,
    UmeaEnergiClient,
)

from custom_components.umea_energi.const import (
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_REFRESH_TOKEN_EXPIRES_AT,
    DOMAIN,
)

from .conftest import CUSTOMER_CODE, make_jwt

METER_POINTS = [
    MeterPoint(
        contract_type=contract_type,
        meter_point_no=number,
        customer_code=CUSTOMER_CODE,
        address="Street 1",
        contract_id=None,
    )
    for contract_type, number in [
        ("ELECTRICITY_GRID", "735"),
        ("ELECTRICITY", "735"),
        ("DISTRICT_HEATING", "900"),
        ("BROADBAND", "1"),
    ]
]
# 48 hourly readings of 1 kWh, covering 22 and 23 October local time (CEST).
FIRST_HOUR = datetime(2026, 10, 21, 22, tzinfo=UTC)
PERIODS = [(datetime(2026, 10, 22) + timedelta(hours=hour)).isoformat() for hour in range(48)]
STATISTIC_ID = f"{DOMAIN}:electricity_consumption_735"
ELECTRICITY = "sensor.umea_energi_electricity_street_1"
DISTRICT_HEATING = "sensor.umea_energi_district_heating_street_1"


async def fake_consumption(
    access_token: str, meter_point: MeterPoint, start: date, end: date, resolution: Resolution
) -> Any:
    assert resolution is Resolution.HOUR
    if meter_point.contract_type == "DISTRICT_HEATING":
        raise ApiError("Internal server error", 500)
    return {"consumption": [{"period": period, "value": 1.0} for period in PERIODS]}


@pytest.fixture(autouse=True)
async def stockholm_time(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to(datetime(2026, 10, 24, 10, tzinfo=UTC))


@pytest.fixture(autouse=True)
def fake_api() -> Any:
    with (
        patch.object(UmeaEnergiClient, "meter_points", return_value=METER_POINTS),
        patch.object(UmeaEnergiClient, "consumption", side_effect=fake_consumption),
    ):
        yield


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def electricity_sums(hass: HomeAssistant) -> list[float | None]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        FIRST_HOUR - timedelta(days=1),
        None,
        {STATISTIC_ID},
        "hour",
        None,
        {"sum"},
    )
    return [row.get("sum") for row in stats.get(STATISTIC_ID, [])]


async def test_sensors_and_statistics(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    await setup_entry(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    latest_day = hass.states.get(f"{ELECTRICITY}_latest_day_consumption")
    month = hass.states.get(f"{ELECTRICITY}_consumption_this_month")
    latest_reading = hass.states.get(f"{ELECTRICITY}_latest_reading")
    assert latest_day is not None
    assert latest_day.state == "24.0"
    assert month is not None
    assert month.state == "48.0"
    assert latest_reading is not None
    assert latest_reading.state == "2026-10-23T21:00:00+00:00"

    # One device per meter: the supply contract shares the grid contract's meter, and broadband
    # has no consumption. District heating failed, so its sensors are unavailable.
    heating = hass.states.get(f"{DISTRICT_HEATING}_latest_day_consumption")
    assert heating is not None
    assert heating.state == STATE_UNAVAILABLE
    assert len(hass.states.async_entity_ids("sensor")) == 6

    await async_wait_recording_done(hass)
    sums = await electricity_sums(hass)
    assert sums == [float(n) for n in range(1, 49)]

    # Re-importing overlapping hours continues the running sum instead of adding to it.
    await mock_config_entry.runtime_data.async_refresh()
    await async_wait_recording_done(hass)
    assert await electricity_sums(hass) == sums


async def test_refreshed_credentials_are_stored(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    refreshed = Credentials(
        access_token=make_jwt(),
        refresh_token="rotated",
        refresh_token_expires_at=datetime(2027, 10, 24, tzinfo=UTC),
    )
    with patch.object(UmeaEnergiClient, "fresh_credentials", return_value=refreshed):
        await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.data[CONF_ACCESS_TOKEN] == refreshed.access_token
    assert mock_config_entry.data[CONF_REFRESH_TOKEN] == "rotated"
    assert mock_config_entry.data[CONF_REFRESH_TOKEN_EXPIRES_AT] == "2027-10-24T00:00:00+00:00"


async def test_expired_session_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    with patch.object(
        UmeaEnergiClient, "fresh_credentials", side_effect=TokenExpiredError("expired")
    ):
        await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_meter_listing_failure_retries_setup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    with patch.object(UmeaEnergiClient, "meter_points", side_effect=ApiError("Down", 503)):
        await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    await setup_entry(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
