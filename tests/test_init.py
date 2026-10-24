"""Tests for setting up the integration, its sensors and statistics."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.umea_energi.api import (
    Reading,
    UmeaEnergiApi,
    UmeaEnergiAuthError,
    UmeaEnergiError,
)
from custom_components.umea_energi.const import DOMAIN, Utility

from .conftest import CUSTOMER_CODE

# Midnight 2026-10-22 in Europe/Stockholm (CEST).
FIRST_HOUR = datetime(2026, 10, 21, 22, tzinfo=UTC)
# 48 hourly readings of 1 kWh, covering 22 and 23 October local time.
READINGS = [Reading(FIRST_HOUR + timedelta(hours=hour), 1.0) for hour in range(48)]
STATISTIC_ID = f"{DOMAIN}:electricity_consumption_{CUSTOMER_CODE}"


async def fake_consumption(
    utility: Utility, customer_code: str, start: datetime, end: datetime
) -> list[Reading]:
    if utility is Utility.DISTRICT_HEATING:
        raise UmeaEnergiError("No district heating")
    return [reading for reading in READINGS if start <= reading.start < end]


@pytest.fixture(autouse=True)
async def stockholm_time(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    await hass.config.async_set_time_zone("Europe/Stockholm")
    freezer.move_to(datetime(2026, 10, 24, 10, tzinfo=UTC))


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
    with patch.object(UmeaEnergiApi, "async_get_consumption", side_effect=fake_consumption):
        await setup_entry(hass, mock_config_entry)
        assert mock_config_entry.state is ConfigEntryState.LOADED

        latest_day = hass.states.get("sensor.umea_energi_electricity_latest_day_consumption")
        month = hass.states.get("sensor.umea_energi_electricity_consumption_this_month")
        latest_reading = hass.states.get("sensor.umea_energi_electricity_latest_reading")
        assert latest_day is not None
        assert latest_day.state == "24.0"
        assert month is not None
        assert month.state == "48.0"
        assert latest_reading is not None
        assert latest_reading.state == "2026-10-23T21:00:00+00:00"
        # The customer has no district heating, so no sensors are created for it.
        assert not hass.states.async_entity_ids("sensor.umea_energi_district_heating")

        await async_wait_recording_done(hass)
        sums = await electricity_sums(hass)
        assert sums == [float(n) for n in range(1, 49)]

        # Re-importing overlapping hours continues the running sum instead of adding to it.
        await mock_config_entry.runtime_data.async_refresh()
        await async_wait_recording_done(hass)
        assert await electricity_sums(hass) == sums


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    with patch.object(
        UmeaEnergiApi, "async_get_consumption", side_effect=UmeaEnergiAuthError("expired")
    ):
        await setup_entry(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_unload(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> None:
    with patch.object(UmeaEnergiApi, "async_get_consumption", side_effect=fake_consumption):
        await setup_entry(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
