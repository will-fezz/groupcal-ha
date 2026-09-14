# GroupCal for Home Assistant

Unofficial custom integration that shows each [GroupCal](https://www.groupcal.app/) group as a Home Assistant calendar entity, with create, edit and delete support.

It talks to the same backend the GroupCal web app uses (`https://www.twentyfour.me/api`). The API is undocumented; every endpoint and payload here was proven against the live service before being ported.

## Install (HACS)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=will-fezz&repository=groupcal-ha&category=integration)

Or by hand: in HACS open **⋮ → Custom repositories**, add `https://github.com/will-fezz/groupcal-ha` with category **Integration**, install **GroupCal**, and restart Home Assistant. Then go to **Settings → Devices & services → Add integration → GroupCal**.

## Getting your setup code

GroupCal has no username and password. Its app logs in once by text message and then keeps a long-lived token. GroupCal only sends that text from its own website (it's protected by reCAPTCHA), so Home Assistant can't do the text-message step for you. Instead you log in on their site once and copy the result across:

1. On a computer, log in at <https://app.groupcal.app> with your phone number and the text-message code.
2. Open the browser console: **F12** (or right-click → Inspect) → **Console**. Chrome may ask you to type `allow pasting` first.
3. Paste this line and press Enter. It copies your setup code to the clipboard and doesn't send it anywhere:

   ```js
   copy(JSON.stringify({phoneNumber:localStorage.phoneNumber,deviceId:localStorage.deviceId,CODE_PROVIDER:localStorage.CODE_PROVIDER,CM_TOKEN:localStorage.CM_TOKEN})),"Copied - paste it into Home Assistant"
   ```

4. Paste the result into the **Setup code** box in Home Assistant.

The setup code holds your login token, so treat it like a password. Don't post it anywhere.

If you'd rather fill the fields in yourself, leave Setup code empty and enter the four values from the web app's local storage:

| Field | localStorage key | Notes |
|---|---|---|
| Phone number | `phoneNumber` | E.164, e.g. `+447700900000` |
| Device ID | `deviceId` | UUID the web app generated for that login |
| Code provider | `CODE_PROVIDER` | `checkmobi` → enter `cm` |
| Verification token | `CM_TOKEN` | Long-lived. Treat it as a password. |

Logins done through Google's phone sign-in (`CODE_PROVIDER` = `firebase`) aren't supported because they don't keep a long-lived token. Log out and back in on the website and it normally uses the text-message route.

## How login works

The integration logs in with `POST /v1/account/login` (`username = phone*deviceId*provider*3`, `password = cmToken`), stores the resulting access token in the config entry, and re-logs in automatically on HTTP 401/403. If GroupCal rejects the login material itself, Home Assistant raises a re-authentication prompt.

## Options

**Configure** on the integration lets you pick which groups become calendars. All discovered groups are included until you choose.

## Behaviour

- Polls every 5 minutes for the next 30 days (plus one day behind). The entity state is on while an event is in progress; attributes show the current or next event.
- The calendar panel and `calendar.get_events` query GroupCal live for whatever range is asked.
- All-day events: GroupCal stores these at midnight of the first day and midnight or 23:59 of the last day, in the event's own `TimeZoneNameID` (often `UTC`). They're converted to Home Assistant's date-only, end-exclusive form.
- Timed events are shown in Home Assistant's configured time zone; writes stamp that time zone on the event.
- Create clones the payload shape of a real event in the same group, then `POST /tasks/add`. Edit fetches the full stored event, changes only the requested fields and sends `POST /v1/task/edit/batch`. Delete does what the app does: sets `Status` to `4` through the edit path.

## Limitations

- Recurring events: creating with a repeat rule is refused. GroupCal's recurrence format has not been mapped; editing or deleting a recurring event acts on the whole stored event.
- Reminders are not exposed through Home Assistant's calendar UI.
- Group discovery uses the full sync feed (`POST /general/changes` from `LastUpdate 0`), which can be large on big accounts.
- If another client shares the same login material, each login may rotate the other's token. Both recover by re-logging in on 403.

## Development

```bash
python -m venv /tmp/ha-venv
/tmp/ha-venv/bin/pip install pytest-homeassistant-custom-component ruff
/tmp/ha-venv/bin/python -m pytest          # mocked API, no network
/tmp/ha-venv/bin/ruff check . && /tmp/ha-venv/bin/ruff format --check .
GROUPCAL_STATE=/path/to/auth.json python tests/live_read_test.py  # READ-ONLY live check; results go to /tmp, never the repo
```
