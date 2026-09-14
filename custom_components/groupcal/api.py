"""Async client for the GroupCal backend (www.twentyfour.me).

Endpoints and payload shapes mirror a client already proven against the live service;
nothing here is guessed.

This module deliberately imports nothing from Home Assistant so it can be
exercised standalone (see tests/live_read_test.py).
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

DEFAULT_BASE_URL = "https://www.twentyfour.me/api"
DEFAULT_TIMEZONE = "Europe/London"

# Event Status values from the GroupCal web app bundle (2026-09-08):
# ACTIVE 1, COMPLETED 2, REMOVED 3, DELETED 4, HARD_DELETE_FROM_DB 5.
EVENT_STATUS_DELETED = "4"
_HIDDEN_STATUSES = {"3", "4", "5"}

# Fields that identify a specific stored document and must not be cloned into a new event.
_IDENTITY_KEYS = (
    "_id",
    "_rev",
    "local_id",
    "ParticipantsStatus",
    "AggregatedParticipantsDeliveryStatus",
    "ThirdPartyID",
)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

RawEvent = dict[str, Any]


class GroupCalError(Exception):
    """Any failure talking to GroupCal."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class GroupCalAuthError(GroupCalError):
    """Login material was rejected; a fresh SMS login is needed."""


@dataclass
class LoginMaterial:
    """What the GroupCal app keeps after its one-time SMS login.

    `cm_token` is the long-lived secret (the SMS verification token); the others
    are identifiers. Together they let us mint fresh access tokens without SMS.
    """

    phone_number: str
    device_id: str
    code_provider: str
    cm_token: str


@dataclass
class GroupCalGroup:
    id: str
    name: str


TokenCallback = Callable[[str, str], Awaitable[None] | None]


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or value in ("1", "true", "True")


def is_deleted_event(raw: RawEvent) -> bool:
    """The app deletes by setting Status=4 via the edit path; also honour isDeleted."""
    if str(raw.get("Status", "")) in _HIDDEN_STATUSES:
        return True
    return _truthy(raw.get("isDeleted"))


def _extract_events(response: Any) -> list[RawEvent]:
    if isinstance(response, dict):
        events = response.get("events")
        if isinstance(events, list):
            return [e for e in events if isinstance(e, dict) and not is_deleted_event(e)]
        for value in response.values():
            nested = _extract_events(value)
            if nested:
                return nested
    return []


def _epoch_string(epoch_seconds: float) -> str:
    return f"{int(epoch_seconds)}.000000"


def resolve_tz(name: Any, fallback: str) -> ZoneInfo:
    if isinstance(name, str) and name and name != "null":
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo(fallback)


def start_of_day_epoch(day: date, timezone: str) -> int:
    """Local midnight at the start of `day` in `timezone`, as epoch seconds (DST-safe)."""
    return int(datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(timezone)).timestamp())


def event_is_all_day(raw: RawEvent) -> bool:
    return _truthy(raw.get("AllDay"))


def event_times(raw: RawEvent, timezone: str) -> tuple[datetime | date, datetime | date]:
    """Convert GroupCal StartDate/EndDate into HA-style start/end.

    Timed events: aware datetimes in `timezone`.
    All-day events: dates, end exclusive. GroupCal stores all-day events as
    midnight of the first day and either midnight or 23:59 of the *last* day
    (single-day events have StartDate == EndDate), in the event's own
    TimeZoneNameID (often "UTC" for app-created events). So the civil date of
    each edge in that zone gives an inclusive range.
    """
    start_epoch = float(raw.get("StartDate") or 0)
    end_epoch = float(raw.get("EndDate") or 0) or start_epoch
    if event_is_all_day(raw):
        ev_tz = resolve_tz(raw.get("TimeZoneNameID"), timezone)
        start_day = datetime.fromtimestamp(start_epoch, ev_tz).date()
        last_day = datetime.fromtimestamp(end_epoch, ev_tz).date()
        if last_day < start_day:
            last_day = start_day
        return start_day, last_day + timedelta(days=1)
    tz = ZoneInfo(timezone)
    start = datetime.fromtimestamp(start_epoch, tz)
    end = datetime.fromtimestamp(end_epoch, tz)
    if end <= start:
        end = start + timedelta(minutes=1)
    return start, end


def event_title(raw: RawEvent) -> str:
    return str(raw.get("Text") or raw.get("Title") or "(untitled)")


def event_location(raw: RawEvent) -> str:
    value = raw.get("Location")
    if not value or value == "null":
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("Address") or value.get("Name") or value.get("Title") or "")
    return ""


def event_notes(raw: RawEvent) -> str:
    notes = raw.get("Notes")
    if not isinstance(notes, list):
        return ""
    parts = [str(n.get("Note")) for n in notes if isinstance(n, dict) and n.get("Note") not in (None, "", "null")]
    return "\n".join(parts)


@dataclass
class EventFields:
    """User-facing fields to apply onto a raw GroupCal event. None = leave alone."""

    title: str | None = None
    start: datetime | date | None = None
    end: datetime | date | None = None
    all_day: bool | None = None
    notes: str | None = None
    location: str | None = None
    reminder_minutes: float | None = None


def apply_event_fields(target: RawEvent, fields: EventFields, timezone: str) -> RawEvent:
    """Mirror of the plugin's applyEventFields, taking date/datetime objects."""
    out: RawEvent = dict(target)
    if fields.title is not None:
        out["Text"] = fields.title

    all_day = fields.all_day if fields.all_day is not None else event_is_all_day(out)
    if fields.start is not None or fields.end is not None or fields.all_day is not None:
        if all_day:
            day = fields.start if fields.start is not None else fields.end
            if isinstance(day, datetime):
                day = day.astimezone(ZoneInfo(timezone)).date()
            start_epoch = start_of_day_epoch(day, timezone) if day else int(float(out.get("StartDate") or 0))
            # HA gives an exclusive end date; GroupCal stores the last day inclusive.
            end_epoch = start_epoch
            if isinstance(fields.end, date) and not isinstance(fields.end, datetime) and day:
                last = fields.end - timedelta(days=1)
                if last > day:
                    end_epoch = start_of_day_epoch(last, timezone)
            out["AllDay"] = "1"
            out["StartDate"] = _epoch_string(start_epoch)
            out["EndDate"] = _epoch_string(end_epoch)
        else:
            prev_start = float(out.get("StartDate") or 0)
            prev_end = float(out.get("EndDate") or 0)
            start_epoch = _to_epoch(fields.start, timezone) if fields.start is not None else prev_start
            end_epoch = _to_epoch(fields.end, timezone) if fields.end is not None else prev_end
            if fields.start is not None and fields.end is None:
                prev_duration = prev_end - prev_start
                end_epoch = start_epoch + (prev_duration if 0 < prev_duration < 7 * 86400 else 3600)
            if end_epoch <= start_epoch:
                raise GroupCalError("Event end must be after start.")
            out["AllDay"] = "0"
            out["StartDate"] = _epoch_string(start_epoch)
            out["EndDate"] = _epoch_string(end_epoch)

    if fields.notes is not None:
        out["Notes"] = (
            [{"FilePath": "null", "Note": fields.notes, "NoteID": int(time.time() * 1000), "Status": 1}]
            if fields.notes.strip()
            else []
        )
    if fields.location is not None:
        out["Location"] = (
            {"Address": fields.location, "Long": "", "Lat": "", "FilteredLocation": fields.location}
            if fields.location.strip()
            else "null"
        )
    if fields.reminder_minutes is not None:
        if fields.reminder_minutes <= 0:
            out["Reminder"] = []
        else:
            offset = round(fields.reminder_minutes * 60)
            start = float(out.get("StartDate") or 0)
            out["Reminder"] = [
                {"isRelativeReminder": "1", "isOn": "1", "offset": offset, "AlertTime": _epoch_string(start - offset)}
            ]
    out["TimeZoneNameID"] = timezone
    return out


def _to_epoch(value: datetime | date, timezone: str) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo(timezone))
        return int(value.timestamp())
    return start_of_day_epoch(value, timezone)


class GroupCalClient:
    """Stateful client: holds login material plus the rotating access token."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        login: LoginMaterial,
        *,
        access_token: str | None = None,
        user_id: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timezone: str = DEFAULT_TIMEZONE,
        on_token_refresh: TokenCallback | None = None,
    ) -> None:
        self._session = session
        self._login = login
        self.access_token = access_token
        self.user_id = user_id
        self._base_url = base_url.rstrip("/")
        self.timezone = timezone
        self._on_token_refresh = on_token_refresh

    # -- transport ---------------------------------------------------------

    async def _request(self, method: str, route: str, body: Any = None, *, auth: bool = True) -> Any:
        headers = {"Content-Type": "application/json"}
        if auth:
            if not self.access_token:
                raise GroupCalError("No access token; login first.", 403)
            headers["Authorization"] = self.access_token
        try:
            async with self._session.request(
                method,
                self._base_url + route,
                headers=headers,
                data=None if body is None else json.dumps(body),
                timeout=_REQUEST_TIMEOUT,
            ) as res:
                text = await res.text()
                if res.status >= 400:
                    # Response bodies never contain our credentials; trim anyway.
                    raise GroupCalError(f"HTTP {res.status} {method} {route.split('?')[0]}: {text[:200]}", res.status)
        except aiohttp.ClientError as err:
            raise GroupCalError(f"{method} {route.split('?')[0]} failed: {err}") from err
        return json.loads(text) if text else None

    async def _authed(
        self, method: str, route: str, body: Any = None, *, body_fn: Callable[[], Any] | None = None
    ) -> Any:
        """Authenticated request; re-login once on 401/403 and retry (plugin: withAuthRetry)."""
        if not self.access_token or not self.user_id:
            await self.async_login()
        try:
            return await self._request(method, route, body_fn() if body_fn else body)
        except GroupCalError as err:
            if err.http_status not in (401, 403):
                raise
        await self.async_login()
        return await self._request(method, route, body_fn() if body_fn else body)

    # -- auth --------------------------------------------------------------

    async def async_login(self) -> None:
        """POST /v1/account/login with the saved login material. Rotates access_token/user_id."""
        m = self._login
        missing = [k for k in ("phone_number", "device_id", "code_provider", "cm_token") if not getattr(m, k)]
        if missing:
            raise GroupCalAuthError(f"Login material missing: {', '.join(missing)}")
        body = {
            "username": f"{m.phone_number}*{m.device_id}*{m.code_provider}*3",
            "password": m.cm_token,
            "secondpassword": None,
            "isWakeUp": True,
        }
        try:
            res = await self._request("POST", "/v1/account/login", body, auth=False)
        except GroupCalError as err:
            if err.http_status is not None and 400 <= err.http_status < 500:
                raise GroupCalAuthError(
                    f"GroupCal rejected the login material (HTTP {err.http_status})", err.http_status
                ) from err
            raise
        if not isinstance(res, dict):
            raise GroupCalAuthError("Unexpected login response")
        token = None
        for key in ("accessToken", "AccessToken", "authToken", "token"):
            if isinstance(res.get(key), str) and res[key]:
                token = res[key]
        user = res.get("user") or res.get("User") or res
        user_id = None
        for key in ("_id", "id", "UserID", "userId"):
            if isinstance(user, dict) and isinstance(user.get(key), str) and user[key]:
                user_id = user[key]
                break
            if isinstance(res.get(key), str) and res[key]:
                user_id = res[key]
                break
        if not token:
            raise GroupCalAuthError("Login response had no access token")
        self.access_token = token
        # The login reply carries only the token; the user id lives on the Account doc in the sync feed.
        self.user_id = user_id or self.user_id or await self._async_fetch_user_id()
        if self._on_token_refresh:
            maybe = self._on_token_refresh(token, user_id)
            if maybe is not None:
                await maybe

    async def _async_fetch_user_id(self) -> str:
        res = await self._request("POST", "/general/changes", {"LastUpdate": "0"})
        profile_user = None
        for item in (res or {}).get("results", []) if isinstance(res, dict) else []:
            doc = item.get("doc") if isinstance(item, dict) else None
            if not isinstance(doc, dict):
                continue
            if doc.get("Type") == "Account" and doc.get("_id"):
                return str(doc["_id"])
            if doc.get("Type") == "Profile" and doc.get("UserID"):
                profile_user = str(doc["UserID"])
        if profile_user:
            return profile_user
        raise GroupCalAuthError("Could not find the GroupCal user id after login")

    # -- reads -------------------------------------------------------------

    async def async_get_groups(self) -> list[GroupCalGroup]:
        """Discover groups from the sync feed (POST /general/changes, LastUpdate 0)."""
        res = await self._authed(
            "POST",
            "/general/changes",
            body_fn=lambda: {"LastUpdate": "0", "DeviceChangeID": self._login.device_id, "UserID": self.user_id},
        )
        found: dict[str, GroupCalGroup] = {}

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                if node.get("_id") and node.get("Type") == "Group" and isinstance(node.get("Name"), str):
                    found[str(node["_id"])] = GroupCalGroup(
                        id=str(node["_id"]), name=node["Name"].strip() or str(node["_id"])
                    )
                for value in node.values():
                    walk(value)

        walk(res)
        return list(found.values())

    async def async_get_events(self, group_id: str, from_epoch: int, to_epoch: int) -> list[RawEvent]:
        """GET /v1/groups/<id>/events?fromDate&toDate — raw, deleted events filtered out."""
        res = await self._authed(
            "GET", f"/v1/groups/{group_id}/events?fromDate={int(from_epoch)}&toDate={int(to_epoch)}"
        )
        return _extract_events(res)

    async def async_find_event(self, group_id: str, event_id: str, near: date | None = None) -> RawEvent:
        anchor = near or datetime.now(ZoneInfo(self.timezone)).date()
        start = start_of_day_epoch(anchor - timedelta(days=400), self.timezone)
        end = start_of_day_epoch(anchor + timedelta(days=400), self.timezone)
        for event in await self.async_get_events(group_id, start, end):
            if str(event.get("_id", "")) == event_id:
                return event
        raise GroupCalError(f"Event {event_id} not found within ±400 days of {anchor}")

    # -- writes ------------------------------------------------------------

    def _batch_body(self, tasks: list[RawEvent]) -> dict[str, Any]:
        return {"DeviceChangeID": self._login.device_id, "UserID": self.user_id, "tasks": tasks}

    def build_create_payload(self, template: RawEvent | None, group_id: str, fields: EventFields) -> RawEvent:
        """Clone the shape of a real event from the same group (plugin: buildCreatePayload)."""
        if not (fields.title or "").strip():
            raise GroupCalError("A title is required to create an event.")
        if fields.start is None:
            raise GroupCalError("A start is required to create an event.")
        now = time.time()
        base = dict(template or {})
        for key in _IDENTITY_KEYS:
            base.pop(key, None)
        seed: RawEvent = {
            "Type": "GroupEvent",
            "ObjectType": "1",
            "TaskType": 1,
            "Status": 1,
            "Priority": 1,
            "Shared": "null",
            "Label": "null",
            "ParentTaskID": "null",
            "BirthdayDetails": "null",
            "ContactDetails": "null",
            "Recurrence": "null",
            "Job": "null",
            "PostStatus": "0",
            "PrivacyStatus": "1",
            "RequestConfirmation": "0",
            **base,
            "GroupID": group_id,
            "UserID": self.user_id,
            "OwnerID": self.user_id,
            "CreatedBy": self.user_id,
            "DeviceChangeID": self._login.device_id,
            "OpenDate": f"{now:.6f}",
            "Rank": f"{now:.6f}",
            "LastUpdate": f"{int(now)}",
            "local_id": f"local{random.randrange(10**12)}",
            "isDeleted": False,
            "Reminder": [],
            "Notes": [],
            "Location": "null",
            "AllDay": "0",
            "StartDate": "0",
            "EndDate": "0",
        }
        all_day = fields.all_day
        if all_day is None:
            all_day = not isinstance(fields.start, datetime)
        return apply_event_fields(seed, EventFields(**{**fields.__dict__, "all_day": all_day}), self.timezone)

    async def async_create_event(self, group_id: str, fields: EventFields) -> Any:
        """POST /tasks/add with the batch wrapper; bare payload fallback on a 4xx validation reply."""
        if not self.user_id:
            await self.async_login()
        now = datetime.now(ZoneInfo(self.timezone)).date()
        recent = await self.async_get_events(
            group_id,
            start_of_day_epoch(now - timedelta(days=180), self.timezone),
            start_of_day_epoch(now + timedelta(days=180), self.timezone),
        )
        template = next(
            (e for e in recent if e.get("Type") == "GroupEvent" and e.get("AllDay") == "0"),
            recent[0] if recent else None,
        )
        payload = self.build_create_payload(template, group_id, fields)

        async def attempt() -> Any:
            try:
                return await self._request("POST", "/tasks/add", self._batch_body([payload]))
            except GroupCalError as err:
                if err.http_status and 400 <= err.http_status < 500 and err.http_status not in (401, 403):
                    return await self._request("POST", "/tasks/add", payload)
                raise

        try:
            return await attempt()
        except GroupCalError as err:
            if err.http_status not in (401, 403):
                raise
        await self.async_login()
        payload["UserID"] = payload["OwnerID"] = payload["CreatedBy"] = self.user_id
        return await attempt()

    async def async_edit_event(self, event: RawEvent) -> Any:
        """POST /v1/task/edit/batch with the full mutated object."""
        return await self._authed(
            "POST",
            "/v1/task/edit/batch",
            body_fn=lambda: self._batch_body([{**event, "LastUpdate": str(int(time.time()))}]),
        )

    async def async_update_event(
        self, group_id: str, event_id: str, fields: EventFields, near: date | None = None
    ) -> Any:
        current = await self.async_find_event(group_id, event_id, near)
        return await self.async_edit_event(apply_event_fields(current, fields, self.timezone))

    async def async_delete_event(self, group_id: str, event_id: str, near: date | None = None) -> Any:
        """Delete exactly as the web app does: Status "4" via the edit path."""
        current = await self.async_find_event(group_id, event_id, near)
        return await self.async_edit_event({**current, "Status": EVENT_STATUS_DELETED})
