"""Supplier comparison: entities, call budget, cache, errors, and the pure helpers."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
import re
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
import yaml

from custom_components.luz_indexada.api import (
    CompareResult,
    LuzApiError,
    parse_all_prices,
    parse_prices,
)
from custom_components.luz_indexada.compare import build_comparison
from custom_components.luz_indexada.const import (
    CONF_COMPARE,
    CONF_INCLUDE_VAT,
    CONF_KVA,
    CONF_MONTHLY_KWH,
    CONF_WINDOW_HOURS,
    DOMAIN,
    STORAGE_VERSION,
)
from custom_components.luz_indexada.diagnostics import async_get_config_entry_diagnostics

from .conftest import BASE, TARIFF, TODAY, TOMORROW, all_tariffs, compare_url, load, prices_url
from .test_coordinator import _at, _setup, _utc

PREFIX = "sensor.luz_indexada_galp_plano_flexivel_simples"
DASHBOARD = Path(__file__).parents[1] / "examples" / "dashboard.yaml"
DASHBOARD_PT = Path(__file__).parents[1] / "examples" / "dashboard.pt.yaml"
COMPARE = load("compare_2026-10.json")


@pytest.fixture
def cmp_entry(entry_data: dict[str, Any]) -> MockConfigEntry:
    """An entry with the comparison on (the default for new and upgraded entries)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Galp Plano Flexível (simples)",
        data=entry_data,
        options={CONF_INCLUDE_VAT: True, CONF_WINDOW_HOURS: 2.0},
        unique_id=f"{TARIFF}_simples",
    )


def _mock(aioclient_mock: AiohttpClientMocker, tomorrow: bool = False, compare: dict | None = None) -> None:
    aioclient_mock.get(prices_url(TODAY), json=all_tariffs(load(f"prices_{TODAY}.json")))
    if tomorrow:
        aioclient_mock.get(prices_url(TOMORROW), json=all_tariffs(load(f"prices_{TOMORROW}.json")))
    else:
        aioclient_mock.get(prices_url(TOMORROW), status=404, json={"detail": "not published yet"})
    aioclient_mock.get(f"{BASE}/v1/tariffs", json=load("tariffs.json"))
    aioclient_mock.get(compare_url(), json=compare or COMPARE)


def _paths(aioclient_mock: AiohttpClientMocker) -> list[str]:
    return [call[1].path for call in aioclient_mock.mock_calls]


async def test_entities_and_call_budget(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, cmp_entry
) -> None:
    """Prices of every tariff come from the same call; compare once a day; withdrawn tariffs left out."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    _mock(aioclient_mock, tomorrow=True)
    await _setup(hass, cmp_entry)
    assert _paths(aioclient_mock) == ["/v1/prices", "/v1/tariffs", "/v1/compare"]
    prices_call, compare_call = aioclient_mock.mock_calls[0][1], aioclient_mock.mock_calls[2][1]
    assert "tariff" not in prices_call.query and prices_call.query["cycle"] == "simples"
    assert dict(compare_call.query) == {"monthly_kwh": "250", "kva": "6.9", "cycle": "simples"}

    # The selected tariff's entities are unchanged.
    price = hass.states.get(f"{PREFIX}_current_price")
    assert float(price.state) == pytest.approx(parse_prices(load(f"prices_{TODAY}.json"), TARIFF).slots[44].total_vat)

    # One average sensor per comparable tariff (the selected one included), with ApexCharts curves.
    repsol = hass.states.get(f"{PREFIX}_average_price_today_repsol_leve_sem_mais")
    galp = hass.states.get(f"{PREFIX}_average_price_today_galp_plano_flexivel")
    g9 = hass.states.get(f"{PREFIX}_average_price_today_g9_smart_dynamic")
    assert repsol.attributes["friendly_name"].endswith("Average price today — Repsol Leve Sem Mais")
    assert float(repsol.state) == pytest.approx(float(galp.state) * 0.9, rel=1e-4)
    assert float(galp.state) == pytest.approx(float(hass.states.get(f"{PREFIX}_today_average_price").state), rel=1e-6)
    assert len(repsol.attributes["raw_today"]) == 100 and repsol.attributes["raw_tomorrow"] == []
    assert set(repsol.attributes["raw_today"][0]) == {"start", "end", "value"}
    assert g9.attributes["estimate"] is True and repsol.attributes["estimate"] is False
    assert galp.attributes["selected"] is True and repsol.attributes["unit_of_measurement"] == "EUR/kWh"
    assert hass.states.get(f"{PREFIX}_average_price_today_coopernico_base_2_0") is None  # withdrawn
    assert hass.states.get(f"{PREFIX}_average_price_today_coopernico_unico") is None  # unavailable

    # Monthly cost per comparable tariff (VAT on: the real 6%/23% split), and the cheapest supplier.
    cost = hass.states.get(f"{PREFIX}_estimated_monthly_cost_repsol_leve_sem_mais")
    assert float(cost.state) == 95.75 and cost.attributes["unit_of_measurement"] == "€"
    assert cost.attributes["saving_vs_selected"] == pytest.approx(92.29 - 95.75)
    assert cost.attributes["monthly_kwh"] == 250 and cost.attributes["kva"] == 6.9
    assert hass.states.get(f"{PREFIX}_estimated_monthly_cost_endesa_tarifa_dinamica").attributes["estimate"] is True
    assert hass.states.get(f"{PREFIX}_estimated_monthly_cost_coopernico_base_2_0") is None
    cheapest = hass.states.get(f"{PREFIX}_cheapest_supplier")
    # Coopérnico Base 2.0 (92.09 €) would win, but it was withdrawn on 2026-09-30.
    assert cheapest.state == "Galp Plano Flexível"
    attrs = cheapest.attributes
    assert [t["tariff"] for t in attrs["tariffs"]] == [
        "galp-plano-flexivel",
        "repsol-leve-sem-mais",
        "g9-smart-dynamic",
        "endesa-dinamica",
    ]
    assert attrs["saving"] == 0 and attrs["selected_cost"] == 92.29 and attrs["cheapest_exact"] == "Galp Plano Flexível"
    assert attrs["tariffs"][0]["selected"] is True and attrs["fetched_day"] == TODAY and attrs["last_error"] is None

    # Same device; the cheapest-supplier summary is a normal sensor.
    registry = er.async_get(hass)
    devices = {e.device_id for e in er.async_entries_for_config_entry(registry, cmp_entry.entry_id)}
    assert len(devices) == 1

    # Quarter-hours, 13:00 (tomorrow is cached) and a reload: no calls.
    aioclient_mock.clear_requests()
    _mock(aioclient_mock, tomorrow=True)
    await _at(hass, freezer, "2026-10-25T10:15:02Z")
    await _at(hass, freezer, "2026-10-25T13:00:02Z")
    assert _paths(aioclient_mock) == ["/v1/prices"]  # tomorrow, every tariff in one call
    assert len(hass.states.get(f"{PREFIX}_average_price_today_repsol_leve_sem_mais").attributes["raw_tomorrow"]) == 96
    assert await hass.config_entries.async_reload(cmp_entry.entry_id)
    await hass.async_block_till_done()
    assert _paths(aioclient_mock) == ["/v1/prices"]

    # Midnight: tomorrow's curves become today's; the compare is refreshed once for the new day.
    aioclient_mock.clear_requests()
    _mock(aioclient_mock)
    await _at(hass, freezer, "2026-10-26T00:00:02Z")
    await _at(hass, freezer, "2026-10-26T00:15:02Z")
    assert _paths(aioclient_mock) == ["/v1/compare"]
    repsol = hass.states.get(f"{PREFIX}_average_price_today_repsol_leve_sem_mais")
    assert len(repsol.attributes["raw_today"]) == 96 and repsol.attributes["raw_tomorrow"] == []

    # Changing the consumption or the power refetches the compare only.
    aioclient_mock.clear_requests()
    _mock(aioclient_mock)
    hass.config_entries.async_update_entry(
        cmp_entry, options={**cmp_entry.options, CONF_MONTHLY_KWH: 400.0, CONF_KVA: "10.35"}
    )
    await hass.async_block_till_done()
    assert _paths(aioclient_mock) == ["/v1/compare"]
    assert dict(aioclient_mock.mock_calls[0][1].query) == {"monthly_kwh": "400", "kva": "10.35", "cycle": "simples"}
    assert hass.states.get(f"{PREFIX}_cheapest_supplier").attributes["monthly_kwh"] == 400

    diag = await async_get_config_entry_diagnostics(hass, cmp_entry)
    assert diag["comparison"]["enabled"] is True and diag["comparison"]["kva"] == "10.35"
    assert "coopernico-base-2" in diag["comparison"]["tariffs_today"] and diag["comparison"]["compare_day"] == TOMORROW
    assert diag["comparison"]["catalogue_fetched"].startswith("2026-10-25T10:00")

    # Without VAT the costs use total_excl_vat_eur.
    hass.config_entries.async_update_entry(cmp_entry, options={**cmp_entry.options, CONF_INCLUDE_VAT: False})
    await hass.async_block_till_done()
    assert float(hass.states.get(f"{PREFIX}_estimated_monthly_cost_galp_plano_flexivel").state) == 82.85


async def test_turning_comparison_on_fills_cached_days_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, cmp_entry
) -> None:
    """Entries cached without the other tariffs refetch each cached day once; off means no extra entities."""
    freezer.move_to(_utc("2026-10-25T15:00:00Z"))
    cmp_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(cmp_entry, options={**cmp_entry.options, CONF_COMPARE: False})
    _mock(aioclient_mock, tomorrow=True)
    await hass.config_entries.async_setup(cmp_entry.entry_id)
    await hass.async_block_till_done()
    assert _paths(aioclient_mock) == ["/v1/prices", "/v1/prices"]
    assert "tariff" in aioclient_mock.mock_calls[0][1].query
    assert hass.states.get(f"{PREFIX}_cheapest_supplier") is None

    aioclient_mock.clear_requests()
    _mock(aioclient_mock, tomorrow=True)
    hass.config_entries.async_update_entry(cmp_entry, options={**cmp_entry.options, CONF_COMPARE: True})
    await hass.async_block_till_done()
    assert _paths(aioclient_mock) == ["/v1/prices", "/v1/prices", "/v1/tariffs", "/v1/compare"]
    await _at(hass, freezer, "2026-10-25T15:15:02Z")
    assert len(aioclient_mock.mock_calls) == 4
    assert hass.states.get(f"{PREFIX}_cheapest_supplier").state == "Galp Plano Flexível"


async def test_compare_errors_never_block_prices(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer: FrozenDateTimeFactory, cmp_entry
) -> None:
    """A failing compare retries after 6 h; cost sensors appear when it works; 401 still asks for a new key."""
    freezer.move_to(_utc("2026-10-25T08:00:00Z"))
    aioclient_mock.get(prices_url(TODAY), json=all_tariffs(load(f"prices_{TODAY}.json")))
    aioclient_mock.get(f"{BASE}/v1/tariffs", json=load("tariffs.json"))
    aioclient_mock.get(compare_url(), status=422, json={"detail": "bad kva"})
    await _setup(hass, cmp_entry)
    assert hass.states.get(f"{PREFIX}_current_price").state not in ("unknown", "unavailable")
    cheapest = hass.states.get(f"{PREFIX}_cheapest_supplier")
    assert cheapest.state == "unknown" and cheapest.attributes["last_error"] == "HTTP 422"
    assert cheapest.attributes["next_attempt"].startswith("2026-10-25T14:00")
    assert hass.states.get(f"{PREFIX}_estimated_monthly_cost_galp_plano_flexivel") is None
    assert hass.states.get(f"{PREFIX}_average_price_today_galp_plano_flexivel") is not None

    await _at(hass, freezer, "2026-10-25T12:00:02Z")
    assert _paths(aioclient_mock) == ["/v1/prices", "/v1/tariffs", "/v1/compare"]

    aioclient_mock.clear_requests()
    aioclient_mock.get(prices_url(TOMORROW), status=404, json={"detail": "not published yet"})
    aioclient_mock.get(compare_url(), json=COMPARE)
    await _at(hass, freezer, "2026-10-25T14:00:02Z")
    assert _paths(aioclient_mock) == ["/v1/prices", "/v1/compare"]  # catalogue is still fresh
    assert float(hass.states.get(f"{PREFIX}_estimated_monthly_cost_galp_plano_flexivel").state) == 92.29

    # A catalogue error is handled the same way; an auth error starts reauth.
    aioclient_mock.clear_requests()
    aioclient_mock.get(compare_url(), status=401)
    aioclient_mock.get(prices_url(TOMORROW), status=404, json={"detail": "not published yet"})
    hass.config_entries.async_update_entry(cmp_entry, options={**cmp_entry.options, CONF_MONTHLY_KWH: 100.0})
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_unreadable_comparison_cache_is_ignored(
    hass: HomeAssistant, hass_storage: dict[str, Any], aioclient_mock: AiohttpClientMocker, freezer, cmp_entry
) -> None:
    """A broken comparison cache keeps the cached prices and refetches only what's missing."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    today = parse_prices(load(f"prices_{TODAY}.json"), TARIFF)
    key = f"{DOMAIN}.{cmp_entry.entry_id}"
    hass_storage[key] = {
        "version": STORAGE_VERSION,
        "key": key,
        "data": {"tariff": TARIFF, "cycle": "simples", "today": today.as_dict(), "peers_today": {"x": {"day": 1}}},
    }
    _mock(aioclient_mock)
    await _setup(hass, cmp_entry)
    assert _paths(aioclient_mock) == ["/v1/prices", "/v1/tariffs", "/v1/compare"]


def test_parse_all_prices_skips_unusable_entries() -> None:
    """Unavailable and empty entries are dropped; the withdrawn flag comes from replaced_by."""
    body = all_tariffs(load(f"prices_{TODAY}.json"))
    body["tariffs"].append({"tariff": "empty", "prices": []})
    body["tariffs"].append({"no": "id"})
    peers = parse_all_prices(body)
    assert set(peers) == {TARIFF, "repsol-leve-sem-mais", "g9-smart-dynamic", "coopernico-base-2", "endesa-dinamica"}
    assert parse_all_prices({"date": TODAY, "tariffs": None}) == {}


def test_compare_result_round_trip_and_bad_rows() -> None:
    """Cache form parses back; rows without totals or unavailable are skipped."""
    raw = load("compare_2026-10.json")
    raw["results"].append({"tariff": "broken", "available": True, "total_eur": None})
    raw["results"].append("nope")
    result = CompareResult.from_dict(raw)
    assert set(result.costs) == {
        "coopernico-base-2",
        "galp-plano-flexivel",
        "repsol-leve-sem-mais",
        "g9-smart-dynamic",
        "endesa-dinamica",
    }
    assert result.kwh == 250 and result.month == "2026-10" and result.notes
    result.fetched_day = TODAY
    again = CompareResult.from_dict(result.as_dict())
    assert again.costs == result.costs and again.fetched_day == TODAY
    with pytest.raises(LuzApiError):
        CompareResult.from_dict({"detail": "x"})
    assert CompareResult.from_dict({"results": []}, kwh=10).kwh == 10


def test_build_comparison_without_costs_or_selected() -> None:
    """No compare yet: curves only; the entry's own replaced_by also drops a tariff."""
    peers = parse_all_prices(all_tariffs(load(f"prices_{TODAY}.json")))
    peers["repsol-leve-sem-mais"].extra.update(replaced_by="other", valid_until="2026-01-01")
    c = build_comparison(
        selected="gone",
        today=date(2026, 10, 25),
        peers_today=peers,
        peers_tomorrow={},
        compare=None,
        catalogue={},
        include_vat=True,
    )
    assert set(c.curves) == {TARIFF, "g9-smart-dynamic", "coopernico-base-2", "endesa-dinamica"}
    assert c.cheapest is None and c.cost(TARIFF) is None and c.saving(TARIFF) is None
    summary = c.summary()
    assert summary["cheapest_tariff"] is None and summary["tariffs"] == [] and summary["notes"] == []
    result = CompareResult.from_dict(load("compare_2026-10.json"))
    result.fetched_at = datetime(2026, 10, 25, tzinfo=UTC)
    c = build_comparison(
        selected="gone",
        today=date(2026, 10, 25),
        peers_today={},
        peers_tomorrow={},
        compare=result,
        catalogue={},
        include_vat=False,
    )
    assert c.cheapest.tariff == "galp-plano-flexivel" and c.saving("galp-plano-flexivel") is None
    assert c.summary()["cheapest_exact"] == "Galp Plano Flexível"


async def test_tariff_gone_and_no_comparison(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, cmp_entry
) -> None:
    """Sensors of a tariff that leaves the comparison become unavailable instead of showing stale data."""
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    _mock(aioclient_mock)
    await _setup(hass, cmp_entry)
    coordinator = cmp_entry.runtime_data
    coordinator.async_set_updated_data(replace(coordinator.data, comparison=None))
    await hass.async_block_till_done()
    avg_id = f"{PREFIX}_average_price_today_repsol_leve_sem_mais"
    cost_id = f"{PREFIX}_estimated_monthly_cost_repsol_leve_sem_mais"
    assert hass.states.get(avg_id).state == "unavailable" and hass.states.get(cost_id).state == "unavailable"
    cheapest = hass.states.get(f"{PREFIX}_cheapest_supplier")
    assert cheapest.state == "unknown" and "tariffs" not in cheapest.attributes
    sensors = hass.data[DATA_INSTANCES]["sensor"]
    for entity_id in (avg_id, cost_id):
        entity = sensors.get_entity(entity_id)
        assert entity.native_value is None and entity.extra_state_attributes is None


def test_tariff_without_priced_slots_has_no_average() -> None:
    """A tariff whose day is all pending shows no average."""
    peers = parse_all_prices(all_tariffs(load(f"prices_{TODAY}.json")))
    day = peers["g9-smart-dynamic"]
    pending = replace(day, slots=[replace(s, total=None, total_vat=None) for s in day.slots])
    c = build_comparison(
        selected=TARIFF,
        today=date(2026, 10, 25),
        peers_today={"g9-smart-dynamic": pending},
        peers_tomorrow={},
        compare=None,
        catalogue={},
        include_vat=True,
    )
    assert c.curves["g9-smart-dynamic"].today_avg is None


def test_estimate_wins_when_catalogue_and_prices_disagree() -> None:
    """The live API lists Endesa as exact in /v1/prices and estimate in /v1/tariffs: show it as estimate."""
    peers = parse_all_prices(load(f"prices_{TODAY}.json"))
    assert peers[TARIFF].status == "exact"
    c = build_comparison(
        selected=TARIFF,
        today=date(2026, 10, 25),
        peers_today=peers,
        peers_tomorrow={},
        compare=None,
        catalogue={TARIFF: {"status": "estimate"}},
        include_vat=True,
    )
    assert c.curves[TARIFF].status == "estimate"


async def test_example_dashboard_entities_exist(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, cmp_entry
) -> None:
    """Every entity id in examples/dashboard.yaml is created by a default install."""
    view = yaml.safe_load(DASHBOARD.read_text())
    assert view["type"] == "sections" and view["sections"]
    found = re.findall(r"\b(?:sensor|binary_sensor)\.luz_indexada_[a-z0-9_]+", DASHBOARD.read_text())
    ids = {i for i in found if not i.endswith("_")}  # the prefix in the comments ends with "_…"
    assert len(ids) >= 15
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    _mock(aioclient_mock)
    await _setup(hass, cmp_entry)
    missing = sorted(i for i in ids if hass.states.get(i) is None)
    assert missing == []


async def test_example_dashboard_pt_entities_exist(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, freezer, cmp_entry
) -> None:
    """Every entity id in examples/dashboard.pt.yaml is created by an install with HA in Portuguese."""
    hass.config.language = "pt"
    text = DASHBOARD_PT.read_text()
    assert yaml.safe_load(text)["type"] == "sections"
    ids = {i for i in re.findall(r"\b(?:sensor|binary_sensor)\.luz_indexada_[a-z0-9_]+", text) if not i.endswith("_")}
    en = {
        i
        for i in re.findall(r"\b(?:sensor|binary_sensor)\.luz_indexada_[a-z0-9_]+", DASHBOARD.read_text())
        if not i.endswith("_")
    }
    assert len(ids) == len(en)
    freezer.move_to(_utc("2026-10-25T10:00:00Z"))
    _mock(aioclient_mock)
    await _setup(hass, cmp_entry)
    missing = sorted(i for i in ids if hass.states.get(i) is None)
    assert missing == []
