"""Data update coordinator for Umeå Energi."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from umea_energi_api_client import TokenExpiredError, UmeaEnergiError

from .api import Meter, Reading, UmeaEnergiSession
from .const import (
    CONF_CUSTOMER_CODE,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    INITIAL_HISTORY,
    LOGGER,
    STATISTICS_REIMPORT_WINDOW,
)
from .statistics import async_import_consumption, async_last_statistic_start, statistic_id

# Always fetch at least this far back, so the latest complete day is covered even when
# readings are published with a delay.
MIN_FETCH_WINDOW = timedelta(days=3)

type UmeaEnergiConfigEntry = ConfigEntry[UmeaEnergiCoordinator]


@dataclass(frozen=True, slots=True)
class MeterData:
    """Consumption summary for one meter."""

    latest_reading: datetime | None
    latest_day: date | None
    latest_day_consumption: float | None
    month_consumption: float | None


class UmeaEnergiCoordinator(DataUpdateCoordinator[dict[str, MeterData]]):
    """Fetches hourly consumption per meter, imports it as statistics and summarizes it.

    Data is keyed by `Meter.key`.
    """

    config_entry: UmeaEnergiConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: UmeaEnergiConfigEntry, session: UmeaEnergiSession
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
        self.session = session
        self.customer_code: str = entry.data[CONF_CUSTOMER_CODE]
        self.meters: list[Meter] = []

    @override
    async def _async_setup(self) -> None:
        """Find the customer's meters; new meters show up after reloading the entry."""
        try:
            self.meters = await self.session.async_meters(self.customer_code)
        except TokenExpiredError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except UmeaEnergiError as err:
            raise UpdateFailed(f"Could not list meters: {err}") from err
        if not self.meters:
            LOGGER.warning(
                "Customer %s has no electricity or district heating meters", self.customer_code
            )

    @override
    async def _async_update_data(self) -> dict[str, MeterData]:
        # Keep the last known data for a meter whose update fails.
        data = dict(self.data or {})
        errors: list[UmeaEnergiError] = []
        for meter in self.meters:
            try:
                data[meter.key] = await self._async_update_meter(meter)
            except TokenExpiredError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except UmeaEnergiError as err:
                LOGGER.warning("Could not update %s consumption: %s", meter.key, err)
                errors.append(err)
        if errors and len(errors) == len(self.meters):
            raise UpdateFailed(str(errors[0])) from errors[0]
        return data

    async def _async_update_meter(self, meter: Meter) -> MeterData:
        now = dt_util.utcnow()
        month_start = dt_util.start_of_local_day().replace(day=1)
        last_imported = await async_last_statistic_start(self.hass, statistic_id(meter))
        resume = (
            last_imported - STATISTICS_REIMPORT_WINDOW
            if last_imported is not None
            else now - INITIAL_HISTORY
        )
        start = min(resume, month_start, now - MIN_FETCH_WINDOW)

        readings = await self.session.async_hourly_consumption(meter, start, now)
        await async_import_consumption(self.hass, meter, readings)
        return summarize(readings, month_start)


def summarize(readings: list[Reading], month_start: datetime) -> MeterData:
    """Summarize hourly readings (oldest first) into the values shown by the sensors."""
    if not readings:
        return MeterData(None, None, None, None)

    latest = readings[-1].start
    # The latest local day for which all hours have been read.
    day_end = dt_util.start_of_local_day(dt_util.as_local(latest + timedelta(hours=1)))
    latest_day = (day_end - timedelta(days=1)).date()
    return MeterData(
        latest_reading=latest,
        latest_day=latest_day,
        latest_day_consumption=sum(
            r.value for r in readings if dt_util.as_local(r.start).date() == latest_day
        ),
        month_consumption=sum(r.value for r in readings if r.start >= month_start),
    )
