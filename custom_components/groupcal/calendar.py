"""Calendar platform: one CalendarEntity per GroupCal group."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.calendar import (
    EVENT_DESCRIPTION,
    EVENT_END,
    EVENT_LOCATION,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import (
    EventFields,
    GroupCalError,
    GroupCalGroup,
    RawEvent,
    event_location,
    event_notes,
    event_times,
    event_title,
)
from .const import DOMAIN
from .coordinator import GroupCalConfigEntry, GroupCalCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GroupCalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(GroupCalCalendar(coordinator, group) for group in coordinator.groups)


def to_calendar_event(raw: RawEvent, timezone: str) -> CalendarEvent:
    start, end = event_times(raw, timezone)
    return CalendarEvent(
        start=start,
        end=end,
        summary=event_title(raw),
        description=event_notes(raw) or None,
        location=event_location(raw) or None,
        uid=str(raw.get("_id") or "") or None,
    )


def _local_bounds(event: CalendarEvent) -> tuple[datetime, datetime]:
    return event.start_datetime_local, event.end_datetime_local


def _fields_from_ha(event: dict[str, Any]) -> EventFields:
    if event.get(EVENT_RRULE):
        raise HomeAssistantError("GroupCal integration does not support recurring events")
    start = event.get(EVENT_START)
    end = event.get(EVENT_END)
    all_day = None
    if start is not None:
        all_day = not isinstance(start, datetime)
    return EventFields(
        title=event.get(EVENT_SUMMARY),
        start=start,
        end=end,
        all_day=all_day,
        notes=event.get(EVENT_DESCRIPTION),
        location=event.get(EVENT_LOCATION),
    )


class GroupCalCalendar(CoordinatorEntity[GroupCalCoordinator], CalendarEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.DELETE_EVENT | CalendarEntityFeature.UPDATE_EVENT
    )

    def __init__(self, coordinator: GroupCalCoordinator, group: GroupCalGroup) -> None:
        super().__init__(coordinator)
        self._group = group
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{group.id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.unique_id}_{group.id}")},
            name=group.name,
            manufacturer="GroupCal",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _tz(self) -> str:
        return self.coordinator.client.timezone

    def _cached_events(self) -> list[CalendarEvent]:
        raws = (self.coordinator.data or {}).get(self._group.id, [])
        events = [to_calendar_event(r, self._tz) for r in raws]
        return sorted(events, key=lambda e: _local_bounds(e)[0])

    @property
    def event(self) -> CalendarEvent | None:
        """The event in progress, or else the next upcoming one."""
        now = dt_util.now()
        for ev in self._cached_events():
            if _local_bounds(ev)[1] > now:
                return ev
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        # Slack of a day each side: all-day events are often stored at UTC midnight.
        from_epoch = int(start_date.timestamp()) - 86400
        to_epoch = int(end_date.timestamp()) + 86400
        try:
            raws = await self.coordinator.client.async_get_events(self._group.id, from_epoch, to_epoch)
        except GroupCalError as err:
            raise HomeAssistantError(f"GroupCal fetch failed: {err}") from err
        out = []
        for raw in raws:
            ev = to_calendar_event(raw, self._tz)
            ev_start, ev_end = _local_bounds(ev)
            if ev_end > start_date and ev_start < end_date:
                out.append(ev)
        return sorted(out, key=lambda e: _local_bounds(e)[0])

    def _near(self, uid: str) -> date | None:
        for raw in (self.coordinator.data or {}).get(self._group.id, []):
            if str(raw.get("_id")) == uid:
                start, _ = event_times(raw, self._tz)
                return start if not isinstance(start, datetime) else start.date()
        return None

    async def async_create_event(self, **kwargs: Any) -> None:
        fields = _fields_from_ha(kwargs)
        try:
            await self.coordinator.client.async_create_event(self._group.id, fields)
        except GroupCalError as err:
            raise HomeAssistantError(f"GroupCal create failed: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        fields = _fields_from_ha(event)
        try:
            await self.coordinator.client.async_update_event(self._group.id, uid, fields, self._near(uid))
        except GroupCalError as err:
            raise HomeAssistantError(f"GroupCal update failed: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        try:
            await self.coordinator.client.async_delete_event(self._group.id, uid, self._near(uid))
        except GroupCalError as err:
            raise HomeAssistantError(f"GroupCal delete failed: {err}") from err
        await self.coordinator.async_request_refresh()
