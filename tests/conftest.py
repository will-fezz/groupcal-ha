"""Fixtures: mocked GroupCal API; no network."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.groupcal.api import GroupCalGroup

GROUPS = [GroupCalGroup(id="g1", name="Alpha"), GroupCalGroup(id="g2", name="Beta")]


def raw_event(_id, text, start, end, all_day="0", tz="Europe/London", **extra):
    return {
        "_id": _id,
        "Text": text,
        "StartDate": f"{int(start)}.000000",
        "EndDate": f"{int(end)}.000000",
        "AllDay": all_day,
        "Status": "1",
        "Type": "GroupEvent",
        "TimeZoneNameID": tz,
        **extra,
    }


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def events_by_group():
    return {"g1": [], "g2": []}


@pytest.fixture
def mock_api(events_by_group):
    async def fake_login(self):
        self.access_token = "tok-new"
        self.user_id = "user-1"
        if self._on_token_refresh:
            self._on_token_refresh(self.access_token, self.user_id)

    async def fake_groups(self):
        if not self.access_token:
            await self.async_login()
        return list(GROUPS)

    async def fake_events(self, group_id, from_epoch, to_epoch):
        return [
            e
            for e in events_by_group.get(group_id, [])
            if float(e["EndDate"]) >= from_epoch - 86400 and float(e["StartDate"]) <= to_epoch
        ]

    with (
        patch("custom_components.groupcal.api.GroupCalClient.async_login", fake_login),
        patch("custom_components.groupcal.api.GroupCalClient.async_get_groups", fake_groups),
        patch("custom_components.groupcal.api.GroupCalClient.async_get_events", fake_events),
        patch("custom_components.groupcal.api.GroupCalClient.async_edit_event", autospec=True) as edit,
        patch("custom_components.groupcal.api.GroupCalClient._request", autospec=True) as req,
    ):
        yield {"edit": edit, "request": req}
