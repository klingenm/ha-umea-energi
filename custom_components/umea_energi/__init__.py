"""The Umeå Energi integration."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from umea_energi_api_client import Credentials

from .api import UmeaEnergiSession, create_client, credentials_from_data, credentials_to_data
from .coordinator import UmeaEnergiConfigEntry, UmeaEnergiCoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: UmeaEnergiConfigEntry) -> bool:
    """Set up Umeå Energi from a config entry."""

    @callback
    def _store_credentials(credentials: Credentials) -> None:
        # A refresh may rotate the refresh token; the old one stops working.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, **credentials_to_data(credentials)}
        )

    session = UmeaEnergiSession(
        create_client(), credentials_from_data(entry.data), _store_credentials
    )
    coordinator = UmeaEnergiCoordinator(hass, entry, session)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UmeaEnergiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
