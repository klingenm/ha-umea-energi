"""Long-term statistics import for hourly consumption, for use in the energy dashboard."""

import re
from collections import defaultdict
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import Meter, Reading
from .const import DOMAIN, Utility

STATISTIC_NAMES = {
    Utility.ELECTRICITY: "electricity consumption",
    Utility.DISTRICT_HEATING: "district heating consumption",
}
# How far before the first imported hour to look for the running sum to continue from.
SUM_LOOKBACK = timedelta(days=7)


def statistic_id(meter: Meter) -> str:
    """External statistic id for a meter's consumption."""
    meter_point = re.sub(r"[^a-z0-9_]", "_", meter.meter_point.meter_point_no.lower())
    return f"{DOMAIN}:{meter.utility}_consumption_{meter_point}"


async def async_last_statistic_start(hass: HomeAssistant, stat_id: str) -> datetime | None:
    """Start of the most recently imported hour, if any."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, set()
    )
    if rows := last.get(stat_id):
        return dt_util.utc_from_timestamp(rows[0]["start"])
    return None


async def async_import_consumption(
    hass: HomeAssistant, meter: Meter, readings: list[Reading]
) -> None:
    """Import hourly readings, continuing the running sum from earlier statistics.

    Hours that were imported before are overwritten, so re-importing a window picks up
    corrected readings.
    """
    hourly: defaultdict[datetime, float] = defaultdict(float)
    for reading in readings:
        hourly[reading.start.replace(minute=0, second=0, microsecond=0)] += reading.value
    if not hourly:
        return

    stat_id = statistic_id(meter)
    first = min(hourly)
    previous = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        first - SUM_LOOKBACK,
        first,
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    rows = previous.get(stat_id)
    total = (rows[-1].get("sum") or 0.0) if rows else 0.0

    statistics: list[StatisticData] = []
    for start in sorted(hourly):
        total += hourly[start]
        statistics.append(StatisticData(start=start, state=hourly[start], sum=total))

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"Umeå Energi {STATISTIC_NAMES[meter.utility]} {meter.location}",
        source=DOMAIN,
        statistic_id=stat_id,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    async_add_external_statistics(hass, metadata, statistics)
