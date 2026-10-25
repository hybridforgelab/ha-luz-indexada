"""Config, options and reauth flows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import build_client
from .api import LuzAuthError, LuzConnectionError, LuzError, LuzRateLimitError
from .const import (
    CONF_BASE_URL,
    CONF_CYCLE,
    CONF_INCLUDE_VAT,
    CONF_MODE,
    CONF_RAPIDAPI_HOST,
    CONF_TARIFF,
    CONF_TARIFF_NAME,
    CONF_WINDOW_HOURS,
    CYCLES,
    DEFAULT_CYCLE,
    DEFAULT_INCLUDE_VAT,
    DEFAULT_RAPIDAPI_HOST,
    DEFAULT_WINDOW_HOURS,
    DOMAIN,
    LOGGER,
    MAX_WINDOW_HOURS,
    MIN_WINDOW_HOURS,
    MODE_DIRECT,
    MODE_RAPIDAPI,
)

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
WINDOW = NumberSelector(
    NumberSelectorConfig(min=MIN_WINDOW_HOURS, max=MAX_WINDOW_HOURS, step=0.25, mode=NumberSelectorMode.BOX)
)


def _selectable(tariffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tariffs worth offering: drop the ones withdrawn and replaced (e.g. Coopérnico Base 2.0 → ÚNICO)."""
    today = dt_util.now().date().isoformat()
    live = [t for t in tariffs if not (t.get("replaced_by") and (t.get("valid_until") or "9999") < today)]
    return live or tariffs


def _options_schema(defaults: Mapping[str, Any]) -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_INCLUDE_VAT, default=defaults.get(CONF_INCLUDE_VAT, DEFAULT_INCLUDE_VAT)): BooleanSelector(),
        vol.Required(CONF_WINDOW_HOURS, default=defaults.get(CONF_WINDOW_HOURS, DEFAULT_WINDOW_HOURS)): WINDOW,
    }


class LuzConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a tariff."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._tariffs: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> LuzOptionsFlow:
        """Options flow."""
        return LuzOptionsFlow()

    async def _validate(self, data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Fetch the tariff catalogue (one API call); map failures to form errors."""
        client = build_client(self.hass, data)
        try:
            return await client.tariffs(), {}
        except LuzAuthError:
            return [], {"base": "invalid_auth"}
        except LuzRateLimitError:
            return [], {"base": "rate_limited"}
        except LuzConnectionError:
            return [], {"base": "cannot_connect"}
        except LuzError as err:
            LOGGER.debug("Unexpected API response: %s", err)
            return [], {"base": "unknown"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose how to reach the API."""
        return self.async_show_menu(step_id="user", menu_options=[MODE_RAPIDAPI, MODE_DIRECT])

    async def async_step_rapidapi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """RapidAPI key (and host, under advanced options)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_MODE: MODE_RAPIDAPI,
                CONF_API_KEY: user_input[CONF_API_KEY].strip(),
                CONF_RAPIDAPI_HOST: user_input.get(CONF_RAPIDAPI_HOST, DEFAULT_RAPIDAPI_HOST).strip(),
            }
            self._tariffs, errors = await self._validate(data)
            if not errors:
                self._data = data
                return await self.async_step_tariff()
        schema: dict[vol.Marker, Any] = {vol.Required(CONF_API_KEY): PASSWORD}
        if self.show_advanced_options:
            schema[vol.Required(CONF_RAPIDAPI_HOST, default=DEFAULT_RAPIDAPI_HOST)] = str
        return self.async_show_form(step_id="rapidapi", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_direct(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Base URL + bearer key (self-hosted or testing)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_MODE: MODE_DIRECT,
                CONF_BASE_URL: user_input[CONF_BASE_URL].strip().rstrip("/"),
                CONF_API_KEY: user_input[CONF_API_KEY].strip(),
            }
            if not data[CONF_BASE_URL].startswith(("https://", "http://")):
                errors[CONF_BASE_URL] = "invalid_url"
            else:
                self._tariffs, errors = await self._validate(data)
            if not errors:
                self._data = data
                return await self.async_step_tariff()
        schema = {
            vol.Required(CONF_BASE_URL): URL,
            vol.Required(CONF_API_KEY): PASSWORD,
        }
        return self.async_show_form(step_id="direct", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the tariff, cycle and display options."""
        errors: dict[str, str] = {}
        by_id = {t["id"]: t for t in self._tariffs}
        if user_input is not None:
            tariff = by_id[user_input[CONF_TARIFF]]
            cycle = user_input[CONF_CYCLE]
            if cycle not in (tariff.get("cycles") or CYCLES):
                errors[CONF_CYCLE] = "cycle_not_offered"
            else:
                await self.async_set_unique_id(f"{tariff['id']}_{cycle}")
                self._abort_if_unique_id_configured()
                name = tariff.get("name") or tariff["id"]
                return self.async_create_entry(
                    title=f"{name} ({cycle})",
                    data={**self._data, CONF_TARIFF: tariff["id"], CONF_TARIFF_NAME: name, CONF_CYCLE: cycle},
                    options={
                        CONF_INCLUDE_VAT: user_input[CONF_INCLUDE_VAT],
                        CONF_WINDOW_HOURS: float(user_input[CONF_WINDOW_HOURS]),
                    },
                )
        offered = _selectable(self._tariffs)
        tariff_options = [
            SelectOptionDict(
                value=t["id"],
                label=f"{t.get('name') or t['id']}{' (estimate)' if t.get('status') == 'estimate' else ''}",
            )
            for t in offered
        ]
        default = next((t["id"] for t in offered if t.get("status") == "exact"), offered[0]["id"])
        schema = {
            vol.Required(CONF_TARIFF, default=default): SelectSelector(
                SelectSelectorConfig(options=tariff_options, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_CYCLE, default=DEFAULT_CYCLE): SelectSelector(
                SelectSelectorConfig(options=list(CYCLES), translation_key="cycle", mode=SelectSelectorMode.DROPDOWN)
            ),
            **_options_schema({}),
        }
        return self.async_show_form(step_id="tariff", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The key was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, CONF_API_KEY: user_input[CONF_API_KEY].strip()}
            _, errors = await self._validate(data)
            if not errors:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): PASSWORD}),
            errors=errors,
        )


class LuzOptionsFlow(OptionsFlow):
    """VAT and cheapest-window length (and the RapidAPI host, advanced)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit options."""
        entry = self.config_entry
        if user_input is not None:
            options = {
                CONF_INCLUDE_VAT: user_input[CONF_INCLUDE_VAT],
                CONF_WINDOW_HOURS: float(user_input[CONF_WINDOW_HOURS]),
            }
            if host := (user_input.get(CONF_RAPIDAPI_HOST) or "").strip():
                options[CONF_RAPIDAPI_HOST] = host
            return self.async_create_entry(data=options)
        schema = _options_schema(entry.options)
        if entry.data.get(CONF_MODE) == MODE_RAPIDAPI and self.show_advanced_options:
            current = (
                entry.options.get(CONF_RAPIDAPI_HOST) or entry.data.get(CONF_RAPIDAPI_HOST) or DEFAULT_RAPIDAPI_HOST
            )
            schema[vol.Required(CONF_RAPIDAPI_HOST, default=current)] = str
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
