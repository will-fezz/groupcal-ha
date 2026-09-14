"""The GroupCal integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import GroupCalAuthError, GroupCalClient, GroupCalError, LoginMaterial
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CM_TOKEN,
    CONF_CODE_PROVIDER,
    CONF_DEVICE_ID,
    CONF_GROUPS,
    CONF_PHONE_NUMBER,
    CONF_USER_ID,
)
from .coordinator import GroupCalConfigEntry, GroupCalCoordinator

PLATFORMS = [Platform.CALENDAR]


def build_client(hass: HomeAssistant, entry: GroupCalConfigEntry) -> GroupCalClient:
    """Client whose token refreshes are persisted back into the entry (never logged)."""

    def _persist(token: str, user_id: str) -> None:
        if entry.data.get(CONF_ACCESS_TOKEN) != token or entry.data.get(CONF_USER_ID) != user_id:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_ACCESS_TOKEN: token, CONF_USER_ID: user_id}
            )

    return GroupCalClient(
        async_get_clientsession(hass),
        LoginMaterial(
            phone_number=entry.data[CONF_PHONE_NUMBER],
            device_id=entry.data[CONF_DEVICE_ID],
            code_provider=entry.data[CONF_CODE_PROVIDER],
            cm_token=entry.data[CONF_CM_TOKEN],
        ),
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        user_id=entry.data.get(CONF_USER_ID),
        timezone=hass.config.time_zone,
        on_token_refresh=_persist,
    )


async def async_setup_entry(hass: HomeAssistant, entry: GroupCalConfigEntry) -> bool:
    client = build_client(hass, entry)
    try:
        groups = await client.async_get_groups()
    except GroupCalAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except GroupCalError as err:
        raise ConfigEntryNotReady(str(err)) from err

    selected = entry.options.get(CONF_GROUPS)
    if selected is not None:
        groups = [g for g in groups if g.id in selected]

    coordinator = GroupCalCoordinator(hass, entry, client, groups)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Token refreshes also update the entry; only reload when the options really changed.
    options_snapshot = dict(entry.options)

    async def _on_update(hass: HomeAssistant, updated: GroupCalConfigEntry) -> None:
        if dict(updated.options) != options_snapshot:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_on_update))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GroupCalConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
