"""Polling coordinator for GroupCal."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import GroupCalAuthError, GroupCalClient, GroupCalError, GroupCalGroup, RawEvent, start_of_day_epoch
from .const import DOMAIN, LOOKAHEAD, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

type GroupCalConfigEntry = ConfigEntry[GroupCalCoordinator]


class GroupCalCoordinator(DataUpdateCoordinator[dict[str, list[RawEvent]]]):
    """Fetches the next ~30 days of events for every selected group."""

    config_entry: GroupCalConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GroupCalConfigEntry,
        client: GroupCalClient,
        groups: list[GroupCalGroup],
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.client = client
        self.groups = groups

    async def _async_update_data(self) -> dict[str, list[RawEvent]]:
        tz = self.client.timezone
        today = dt_util.now().date()
        # One day of slack behind: all-day events are often stored at UTC midnight.
        start = start_of_day_epoch(today - timedelta(days=1), tz)
        end = start_of_day_epoch(today + LOOKAHEAD, tz)
        data: dict[str, list[RawEvent]] = {}
        failures: list[str] = []
        for group in self.groups:
            try:
                data[group.id] = await self.client.async_get_events(group.id, start, end)
            except GroupCalAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except GroupCalError as err:
                failures.append(f"{group.name}: {err}")
                if self.data and group.id in self.data:
                    data[group.id] = self.data[group.id]  # keep last good copy
        if failures and len(failures) == len(self.groups):
            raise UpdateFailed("; ".join(failures))
        for failure in failures:
            _LOGGER.warning("GroupCal fetch failed for %s", failure)
        return data
