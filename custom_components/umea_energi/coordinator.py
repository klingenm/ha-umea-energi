"""Data update coordinator for Umeå Energi."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import Reading, UmeaEnergiApi, UmeaEnergiAuthError, UmeaEnergiError
from .const import (
    CONF_CUSTOMER_CODE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    INITIAL_HISTORY,
    LOGGER,
    STATISTICS_REIMPORT_WINDOW,
    Utility,
)
from .statistics import async_import_consumption, async_last_statistic_start, statistic_id

# Always fetch at least this far back, so the latest complete day is covered even when
# readings are published with a delay.
MIN_FETCH_WINDOW = timedelta(days=3)

type UmeaEnergiConfigEntry = ConfigEntry[UmeaEnergiCoordinator]


@dataclass(frozen=True, slots=True)
class UtilityData:
    """Consumption summary for one utility."""

    latest_reading: datetime | None
    latest_day: date | None
    latest_day_consumption: float | None
    month_consumption: float | None


class UmeaEnergiCoordinator(DataUpdateCoordinator[dict[Utility, UtilityData]]):
    """Fetches hourly consumption, imports it as statistics and summarizes it for sensors."""

    config_entry: UmeaEnergiConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: UmeaEnergiConfigEntry, api: UmeaEnergiApi
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
            ),
        )
        self.api = api
        self.customer_code: str = entry.data[CONF_CUSTOMER_CODE]

    async def _async_update_data(self) -> dict[Utility, UtilityData]:
        # Keep the last known data for a utility whose update fails, e.g. a customer
        # without district heating.
        data = dict(self.data or {})
        errors: list[UmeaEnergiError] = []
        for utility in Utility:
            try:
                data[utility] = await self._async_update_utility(utility)
            except UmeaEnergiAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except UmeaEnergiError as err:
                LOGGER.warning("Could not update %s consumption: %s", utility, err)
                errors.append(err)
        if len(errors) == len(Utility):
            raise UpdateFailed(str(errors[0])) from errors[0]
        return data

    async def _async_update_utility(self, utility: Utility) -> UtilityData:
        now = dt_util.utcnow()
        month_start = dt_util.start_of_local_day().replace(day=1)
        last_imported = await async_last_statistic_start(
            self.hass, statistic_id(utility, self.customer_code)
        )
        resume = (
            last_imported - STATISTICS_REIMPORT_WINDOW
            if last_imported is not None
            else now - INITIAL_HISTORY
        )
        start = min(resume, month_start, now - MIN_FETCH_WINDOW)

        readings = await self.api.async_get_consumption(utility, self.customer_code, start, now)
        readings = [reading for reading in readings if reading.start < now]
        await async_import_consumption(self.hass, utility, self.customer_code, readings)
        return summarize(readings, month_start)


def summarize(readings: list[Reading], month_start: datetime) -> UtilityData:
    """Summarize hourly readings (oldest first) into the values shown by the sensors."""
    if not readings:
        return UtilityData(None, None, None, None)

    latest = readings[-1].start
    # The latest local day for which all hours have been read.
    day_end = dt_util.start_of_local_day(dt_util.as_local(latest + timedelta(hours=1)))
    latest_day = (day_end - timedelta(days=1)).date()
    return UtilityData(
        latest_reading=latest,
        latest_day=latest_day,
        latest_day_consumption=sum(
            r.value for r in readings if dt_util.as_local(r.start).date() == latest_day
        ),
        month_consumption=sum(r.value for r in readings if r.start >= month_start),
    )
