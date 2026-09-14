"""READ-ONLY live check of custom_components/groupcal/api.py against real GroupCal.

Loads api.py standalone (no Home Assistant), authenticates from a local JSON
auth state file (keys: phoneNumber, deviceId, codeProvider, cmToken; optionally
accessToken, userId, ignoredGroupIds), lists groups and fetches the next 7 days
(local midnight today to local midnight +7) for non-ignored groups.
Never prints credentials. Never calls a write endpoint.

Results go to stdout and a JSON file that must live outside the repository
(default under /tmp). Real calendar contents must never be committed.

If GroupCal forces a re-login, the rotated access token is written back to the
state file (atomic, mode 600) so other clients sharing it keep working.

    GROUPCAL_STATE=/path/to/auth.json python tests/live_read_test.py [--json /tmp/out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
TZ = "Europe/London"


def load_api():
    spec = importlib.util.spec_from_file_location("groupcal_api", ROOT / "custom_components/groupcal/api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["groupcal_api"] = mod
    spec.loader.exec_module(mod)
    return mod


def save_state(path: str, state: dict) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("GROUPCAL_STATE"), help="auth state JSON (or $GROUPCAL_STATE)")
    ap.add_argument("--json", default="/tmp/groupcal-live-read.json", help="results file (outside the repo)")
    args = ap.parse_args()
    if not args.state:
        ap.error("pass --state or set GROUPCAL_STATE")
    if Path(args.json).resolve().is_relative_to(ROOT):
        ap.error("--json must be outside the repository")

    api = load_api()
    with open(args.state) as fh:
        state = json.load(fh)

    def persist(token: str, user_id: str) -> None:
        state["accessToken"] = token
        state["userId"] = user_id
        save_state(args.state, state)
        print("(access token was refreshed and saved back to the state file)")

    async with aiohttp.ClientSession() as session:
        client = api.GroupCalClient(
            session,
            api.LoginMaterial(state["phoneNumber"], state["deviceId"], state["codeProvider"], state["cmToken"]),
            access_token=state.get("accessToken"),
            user_id=state.get("userId"),
            timezone=TZ,
            on_token_refresh=persist,
        )
        groups = await client.async_get_groups()
        print(f"Groups discovered: {len(groups)}")
        ignored = set(state.get("ignoredGroupIds") or [])
        for g in groups:
            print(f"  - {g.name}{'  [ignored]' if g.id in ignored else ''}")

        # next7: local midnight today -> local midnight today+7.
        today = datetime.now(ZoneInfo(TZ)).date()
        start = api.start_of_day_epoch(today, TZ)
        end = api.start_of_day_epoch(today + timedelta(days=7), TZ)
        print(f"\nWindow {start}..{end} ({today} to {today + timedelta(days=7)} local midnight)")

        result: dict[str, list[str]] = {}
        total = 0
        for g in groups:
            if g.id in ignored:
                continue
            events = await client.async_get_events(g.id, start, end)
            total += len(events)
            result[g.name] = [api.event_title(e) for e in events]
            print(f"\n{g.name}: {len(events)} event(s)")
            for e in events:
                s, en = api.event_times(e, TZ)
                kind = "all-day" if api.event_is_all_day(e) else "timed"
                print(f"  {s} -> {en} [{kind}] {api.event_title(e)}")
        print(f"\nTOTAL {total}")
        if args.json:
            with open(args.json, "w") as fh:
                json.dump({"total": total, "groups": result}, fh, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
