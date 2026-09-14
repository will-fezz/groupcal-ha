"""Config and options flow for GroupCal."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import GroupCalAuthError, GroupCalClient, GroupCalError, GroupCalGroup, LoginMaterial
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CM_TOKEN,
    CONF_CODE_PROVIDER,
    CONF_DEVICE_ID,
    CONF_GROUPS,
    CONF_PHONE_NUMBER,
    CONF_USER_ID,
    DEFAULT_CODE_PROVIDER,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

CONF_SETUP_CODE = "setup_code"
_LOGIN_KEYS = (CONF_PHONE_NUMBER, CONF_DEVICE_ID, CONF_CODE_PROVIDER, CONF_CM_TOKEN)

# Keys the GroupCal web app keeps in localStorage, and what they map to here.
_WEB_KEYS = {
    "phoneNumber": CONF_PHONE_NUMBER,
    "deviceId": CONF_DEVICE_ID,
    "CODE_PROVIDER": CONF_CODE_PROVIDER,
    "CM_TOKEN": CONF_CM_TOKEN,
}


class InvalidSetupCode(ValueError):
    """The pasted setup code could not be read."""


def parse_setup_code(code: str) -> dict[str, str]:
    """Turn the JSON copied from the GroupCal web app into login material.

    Accepts the web app's own localStorage key names or this integration's keys.
    """
    try:
        raw = json.loads(code.strip())
    except ValueError as err:
        raise InvalidSetupCode from err
    if not isinstance(raw, dict):
        raise InvalidSetupCode
    data = {_WEB_KEYS.get(k, k): str(v).strip() for k, v in raw.items() if v not in (None, "")}
    # The web app stores the provider name; the login username uses its short code.
    provider = data.get(CONF_CODE_PROVIDER, DEFAULT_CODE_PROVIDER).lower()
    if provider == "firebase":
        raise InvalidSetupCode  # only SMS logins via Checkmobi carry a long-lived token
    data[CONF_CODE_PROVIDER] = "cm" if provider in ("checkmobi", "cm") else provider
    if not all(data.get(k) for k in _LOGIN_KEYS):
        raise InvalidSetupCode
    return {k: data[k] for k in _LOGIN_KEYS}


def _login_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_SETUP_CODE): _PASSWORD,
            vol.Optional(CONF_PHONE_NUMBER, default=defaults.get(CONF_PHONE_NUMBER, vol.UNDEFINED)): str,
            vol.Optional(CONF_DEVICE_ID, default=defaults.get(CONF_DEVICE_ID, vol.UNDEFINED)): str,
            vol.Optional(CONF_CODE_PROVIDER, default=defaults.get(CONF_CODE_PROVIDER, DEFAULT_CODE_PROVIDER)): str,
            vol.Optional(CONF_CM_TOKEN): _PASSWORD,
        }
    )


def _safe_suggestions(user_input: Mapping[str, Any] | None) -> dict[str, Any]:
    """Re-fill the form after an error, but never echo secrets back."""
    return {k: v for k, v in (user_input or {}).items() if k not in (CONF_SETUP_CODE, CONF_CM_TOKEN)}


def _login_from_input(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Prefer a pasted setup code; otherwise require the four separate fields."""
    if code := str(user_input.get(CONF_SETUP_CODE) or "").strip():
        return parse_setup_code(code)
    data = {k: str(user_input.get(k) or "").strip() for k in _LOGIN_KEYS}
    if not all(data.values()):
        raise InvalidSetupCode
    return data


async def _validate(hass: HomeAssistant, data: Mapping[str, Any]) -> tuple[GroupCalClient, list[GroupCalGroup]]:
    client = GroupCalClient(
        async_get_clientsession(hass),
        LoginMaterial(
            phone_number=data[CONF_PHONE_NUMBER].strip(),
            device_id=data[CONF_DEVICE_ID].strip(),
            code_provider=data[CONF_CODE_PROVIDER].strip(),
            cm_token=data[CONF_CM_TOKEN].strip(),
        ),
        timezone=hass.config.time_zone,
    )
    await client.async_login()
    return client, await client.async_get_groups()


class GroupCalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Take the login material the GroupCal app keeps after its SMS login."""

    VERSION = 1

    async def _try(self, user_input: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
        errors: dict[str, str] = {}
        try:
            login = _login_from_input(user_input)
            client, _groups = await _validate(self.hass, login)
        except InvalidSetupCode:
            errors["base"] = "invalid_setup_code"
        except GroupCalAuthError:
            errors["base"] = "invalid_auth"
        except GroupCalError:
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating GroupCal login")
            errors["base"] = "unknown"
        else:
            data = dict(login)
            data[CONF_ACCESS_TOKEN] = client.access_token
            data[CONF_USER_ID] = client.user_id
            return data, errors
        return None, errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._try(user_input)
            if data:
                await self.async_set_unique_id(data[CONF_USER_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"GroupCal ({data[CONF_PHONE_NUMBER]})", data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_login_schema({}), _safe_suggestions(user_input)),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await self._try(user_input)
            if data:
                await self.async_set_unique_id(data[CONF_USER_ID])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data={**entry.data, **data})
        return self.async_show_form(step_id="reauth_confirm", data_schema=_login_schema(entry.data), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> GroupCalOptionsFlow:
        return GroupCalOptionsFlow()


class GroupCalOptionsFlow(OptionsFlow):
    """Pick which GroupCal groups become calendar entities."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={CONF_GROUPS: user_input[CONF_GROUPS]})

        from . import build_client  # local import: avoids a cycle at module load

        try:
            groups = await build_client(self.hass, self.config_entry).async_get_groups()
        except GroupCalError:
            return self.async_abort(reason="cannot_connect")
        current = self.config_entry.options.get(CONF_GROUPS, [g.id for g in groups])
        schema = vol.Schema(
            {
                vol.Required(CONF_GROUPS, default=[g for g in current if g in {x.id for x in groups}]): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=g.id, label=g.name)
                            for g in sorted(groups, key=lambda g: g.name.lower())
                        ],
                        multiple=True,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
