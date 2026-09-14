"""Config flow, options flow and calendar entity tests with a mocked GroupCal API."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.groupcal.api import GroupCalAuthError, GroupCalClient, LoginMaterial
from custom_components.groupcal.const import DOMAIN

from .conftest import raw_event

_REAL_LOGIN = GroupCalClient.async_login  # captured before fixtures patch it

LOGIN = {
    "phone_number": "+440000000000",
    "device_id": "dev-uuid",
    "code_provider": "cm",
    "cm_token": "SECRET-TOKEN",
}
TZ = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
async def london(hass: HomeAssistant):
    await hass.config.async_set_time_zone("Europe/London")


def _midnight(days: int) -> datetime:
    today = dt_util.now().date() + timedelta(days=days)
    return datetime(today.year, today.month, today.day, tzinfo=TZ)


async def _setup_entry(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user-1",
        data={**LOGIN, "access_token": "tok-old", "user_id": "user-1"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_user_flow_creates_entry(hass: HomeAssistant, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert set(result["data_schema"].schema) >= {"phone_number", "device_id", "code_provider", "cm_token"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == "user-1"
    assert result["data"]["access_token"] == "tok-new"
    assert result["data"]["cm_token"] == "SECRET-TOKEN"
    assert "SECRET" not in result["title"]


async def test_user_flow_invalid_auth(hass: HomeAssistant, mock_api, monkeypatch) -> None:
    async def reject(self):
        raise GroupCalAuthError("rejected", 400)

    monkeypatch.setattr("custom_components.groupcal.api.GroupCalClient.async_login", reject)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_account_aborts(hass: HomeAssistant, mock_api) -> None:
    await _setup_entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], LOGIN)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_entities_and_current_event(hass: HomeAssistant, mock_api, events_by_group) -> None:
    now = dt_util.now()
    events_by_group["g1"] = [
        raw_event("e1", "Checkup", (now + timedelta(hours=2)).timestamp(), (now + timedelta(hours=3)).timestamp()),
        raw_event("e0", "Already over", (now - timedelta(hours=3)).timestamp(), (now - timedelta(hours=2)).timestamp()),
    ]
    await _setup_entry(hass)

    alpha = hass.states.get("calendar.alpha")
    beta = hass.states.get("calendar.beta")
    assert alpha is not None and beta is not None
    assert alpha.state == "off"  # next event is in the future
    assert alpha.attributes["message"] == "Checkup"
    assert beta.attributes.get("message") is None
    assert alpha.attributes["supported_features"] == 7  # create | delete | update


async def test_event_in_progress_turns_on(hass: HomeAssistant, mock_api, events_by_group) -> None:
    now = dt_util.now()
    events_by_group["g1"] = [
        raw_event("e1", "Meeting", (now - timedelta(minutes=10)).timestamp(), (now + timedelta(minutes=50)).timestamp())
    ]
    await _setup_entry(hass)
    assert hass.states.get("calendar.alpha").state == "on"


async def test_get_events_all_day_conversion(hass: HomeAssistant, mock_api, events_by_group) -> None:
    d2 = _midnight(2)
    utc_midnight = datetime(d2.year, d2.month, d2.day, tzinfo=ZoneInfo("UTC"))
    events_by_group["g1"] = [
        # single day stored the way the app does it: UTC midnight, StartDate == EndDate
        raw_event("a1", "Open day", utc_midnight.timestamp(), utc_midnight.timestamp(), all_day="1", tz="UTC"),
        # three-day event stored local midnight -> 23:59:59 on the last day
        raw_event(
            "a2", "Conference", _midnight(3).timestamp(), (_midnight(6) - timedelta(seconds=1)).timestamp(), all_day="1"
        ),
        raw_event(
            "t1",
            "Swim class",
            (_midnight(1) + timedelta(hours=18, minutes=30)).timestamp(),
            (_midnight(1) + timedelta(hours=19, minutes=30)).timestamp(),
        ),
    ]
    await _setup_entry(hass)
    resp = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": "calendar.alpha", "start_date_time": _midnight(0), "end_date_time": _midnight(8)},
        blocking=True,
        return_response=True,
    )
    events = {e["summary"]: e for e in resp["calendar.alpha"]["events"]}
    assert events["Open day"]["start"] == _midnight(2).date().isoformat()
    assert events["Open day"]["end"] == _midnight(3).date().isoformat()
    assert events["Conference"]["start"] == _midnight(3).date().isoformat()
    assert events["Conference"]["end"] == _midnight(6).date().isoformat()
    assert events["Swim class"]["start"] == (_midnight(1) + timedelta(hours=18, minutes=30)).isoformat()


async def test_delete_sets_status_4(hass: HomeAssistant, mock_api, events_by_group) -> None:
    now = dt_util.now()
    events_by_group["g1"] = [
        raw_event("e1", "Checkup", (now + timedelta(hours=2)).timestamp(), (now + timedelta(hours=3)).timestamp())
    ]
    await _setup_entry(hass)
    entity = hass.data["entity_components"]["calendar"].get_entity("calendar.alpha")
    await entity.async_delete_event("e1")
    sent = mock_api["edit"].call_args.args[1]
    assert sent["_id"] == "e1" and sent["Status"] == "4"


async def test_update_mutates_only_requested_fields(hass: HomeAssistant, mock_api, events_by_group) -> None:
    start = _midnight(1) + timedelta(hours=9)
    events_by_group["g1"] = [
        raw_event("e1", "Checkup", start.timestamp(), (start + timedelta(hours=1)).timestamp(), Custom="keep-me")
    ]
    await _setup_entry(hass)
    entity = hass.data["entity_components"]["calendar"].get_entity("calendar.alpha")
    new_start = start + timedelta(hours=2)
    await entity.async_update_event(
        "e1", {"summary": "Checkup (moved)", "dtstart": new_start, "dtend": new_start + timedelta(minutes=30)}
    )
    sent = mock_api["edit"].call_args.args[1]
    assert sent["Text"] == "Checkup (moved)"
    assert sent["StartDate"] == f"{int(new_start.timestamp())}.000000"
    assert sent["EndDate"] == f"{int((new_start + timedelta(minutes=30)).timestamp())}.000000"
    assert sent["AllDay"] == "0"
    assert sent["Custom"] == "keep-me"
    assert sent["TimeZoneNameID"] == "Europe/London"


async def test_create_all_day_uses_tasks_add(hass: HomeAssistant, mock_api, events_by_group) -> None:
    template_start = _midnight(-5) + timedelta(hours=10)
    events_by_group["g1"] = [
        raw_event(
            "tpl",
            "Template",
            template_start.timestamp(),
            (template_start + timedelta(hours=1)).timestamp(),
            _rev="1-abc",
            ColorCode="x",
        )
    ]
    await _setup_entry(hass)
    entity = hass.data["entity_components"]["calendar"].get_entity("calendar.alpha")
    day = _midnight(4).date()
    await entity.async_create_event(summary="Recycling", dtstart=day, dtend=day + timedelta(days=1))
    self_, method, route, body = mock_api["request"].call_args.args
    assert (method, route) == ("POST", "/tasks/add")
    task = body["tasks"][0]
    assert body["UserID"] == "user-1" and body["DeviceChangeID"] == "dev-uuid"
    assert task["Text"] == "Recycling" and task["AllDay"] == "1"
    assert task["StartDate"] == task["EndDate"] == f"{int(_midnight(4).timestamp())}.000000"
    assert task["GroupID"] == "g1" and task["ColorCode"] == "x"
    assert "_id" not in task and "_rev" not in task


async def test_create_rejects_rrule(hass: HomeAssistant, mock_api) -> None:
    from homeassistant.exceptions import HomeAssistantError

    await _setup_entry(hass)
    entity = hass.data["entity_components"]["calendar"].get_entity("calendar.alpha")
    with pytest.raises(HomeAssistantError):
        await entity.async_create_event(
            summary="x", dtstart=_midnight(1).date(), dtend=_midnight(2).date(), rrule="FREQ=WEEKLY"
        )
    mock_api["request"].assert_not_called()


async def test_options_flow_limits_groups(hass: HomeAssistant, mock_api) -> None:
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"groups": ["g2"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get("calendar.beta") is not None
    assert hass.states.get("calendar.alpha") is None or hass.states.get("calendar.alpha").state == "unavailable"


async def test_token_refresh_persists_without_reload(hass: HomeAssistant, mock_api) -> None:
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data
    await coordinator.client.async_login()  # simulate a 403-driven re-login
    await hass.async_block_till_done()
    assert entry.data["access_token"] == "tok-new"
    assert entry.runtime_data is coordinator  # not reloaded


async def test_auth_failure_starts_reauth(hass: HomeAssistant, mock_api, monkeypatch) -> None:
    async def reject(self, *a, **k):
        raise GroupCalAuthError("rejected", 400)

    monkeypatch.setattr("custom_components.groupcal.api.GroupCalClient.async_get_groups", reject)
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-1", data={**LOGIN, "access_token": "t", "user_id": "user-1"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_user_flow_accepts_setup_code(hass: HomeAssistant, mock_api) -> None:
    import json

    code = json.dumps(
        {
            "phoneNumber": LOGIN["phone_number"],
            "deviceId": LOGIN["device_id"],
            "CODE_PROVIDER": "checkmobi",
            "CM_TOKEN": LOGIN["cm_token"],
        }
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"setup_code": code})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["code_provider"] == "cm"
    assert result["data"]["cm_token"] == LOGIN["cm_token"]
    assert "setup_code" not in result["data"]


async def test_user_flow_rejects_bad_setup_code(hass: HomeAssistant, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    firebase = '{"phoneNumber":"+1","deviceId":"d","CODE_PROVIDER":"firebase","CM_TOKEN":"x"}'
    for bad in ("not json", '{"phoneNumber": "+447700900000"}', firebase):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"setup_code": bad})
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_setup_code"}


async def test_login_reads_user_id_from_account_doc() -> None:
    """The login reply has only authToken; the user id comes from the Account doc in the sync feed."""
    calls = []

    async def fake_request(self, method, route, body=None, *, auth=True):
        calls.append(route)
        if route == "/v1/account/login":
            return {"Login Status": "ok", "authToken": "Bearer tok"}
        return {
            "results": [
                {"doc": {"Type": "Group", "_id": "g1", "UserID": "u-9"}},
                {"doc": {"Type": "Account", "_id": "u-9"}},
            ]
        }

    with patch.object(GroupCalClient, "_request", fake_request):
        client = GroupCalClient(None, LoginMaterial(**LOGIN), timezone="Europe/London")
        await _REAL_LOGIN(client)
    assert client.access_token == "Bearer tok"
    assert client.user_id == "u-9"
    assert calls == ["/v1/account/login", "/general/changes"]
