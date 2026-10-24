"""Consumption sensors for Umeå Energi."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, Utility
from .coordinator import UmeaEnergiConfigEntry, UmeaEnergiCoordinator, UtilityData

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class UmeaEnergiSensorEntityDescription(SensorEntityDescription):
    """Describes an Umeå Energi sensor."""

    value_fn: Callable[[UtilityData], float | datetime | None]


# Readings arrive with a delay of up to a day, so these sensors have no state class: hourly
# consumption is imported as external statistics for the energy dashboard instead.
SENSORS: tuple[UmeaEnergiSensorEntityDescription, ...] = (
    UmeaEnergiSensorEntityDescription(
        key="latest_day_consumption",
        translation_key="latest_day_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda data: data.latest_day_consumption,
    ),
    UmeaEnergiSensorEntityDescription(
        key="month_consumption",
        translation_key="month_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda data: data.month_consumption,
    ),
    UmeaEnergiSensorEntityDescription(
        key="latest_reading",
        translation_key="latest_reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.latest_reading,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UmeaEnergiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for the utilities the customer has data for."""
    coordinator = entry.runtime_data
    async_add_entities(
        UmeaEnergiSensor(coordinator, utility, description)
        for utility in coordinator.data
        for description in SENSORS
    )


class UmeaEnergiSensor(CoordinatorEntity[UmeaEnergiCoordinator], SensorEntity):
    """A consumption value for one utility."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: UmeaEnergiCoordinator,
        utility: Utility,
        description: UmeaEnergiSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self.utility = utility
        self._value_fn = description.value_fn
        self._attr_unique_id = f"{coordinator.customer_code}_{utility}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.customer_code}_{utility}")},
            translation_key=utility,
            manufacturer="Umeå Energi",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._update_value()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_value()
        super()._handle_coordinator_update()

    def _update_value(self) -> None:
        if (data := self.coordinator.data.get(self.utility)) is not None:
            self._attr_native_value = self._value_fn(data)
