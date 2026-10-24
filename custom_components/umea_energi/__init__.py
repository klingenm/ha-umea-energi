"""The Umeå Energi integration."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .api import Tokens, UmeaEnergiApi, create_http_client
from .const import CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN
from .coordinator import UmeaEnergiConfigEntry, UmeaEnergiCoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: UmeaEnergiConfigEntry) -> bool:
    """Set up Umeå Energi from a config entry."""
    http = create_http_client()
    entry.async_on_unload(http.aclose)

    @callback
    def _store_tokens(tokens: Tokens) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
            },
        )

    api = UmeaEnergiApi(
        http,
        Tokens(entry.data[CONF_ACCESS_TOKEN], entry.data.get(CONF_REFRESH_TOKEN)),
        on_tokens_refreshed=_store_tokens,
    )
    coordinator = UmeaEnergiCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UmeaEnergiConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
