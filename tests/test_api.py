"""API client and response parsing."""

from __future__ import annotations

from datetime import UTC, date, datetime

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.luz_indexada.api import (
    DayPrices,
    LuzApiClient,
    LuzApiError,
    LuzAuthError,
    LuzConnectionError,
    LuzNotPublishedError,
    LuzRateLimitError,
    parse_prices,
    parse_utc,
)
from custom_components.luz_indexada.const import DEFAULT_RAPIDAPI_HOST, MODE_DIRECT, MODE_RAPIDAPI

from .conftest import BASE, TARIFF, TODAY, TOMORROW, load, prices_url


def test_parse_today_dst_day() -> None:
    """25 Oct 2026 has 100 quarter-hours (the repeated 01:00 hour)."""
    day = parse_prices(load(f"prices_{TODAY}.json"), TARIFF)
    assert day.day == date(2026, 10, 25)
    assert len(day.slots) == 100 and day.priced == 100 and day.complete
    assert day.status == "exact" and day.tariff_name == "Galp Plano Flexível"
    assert day.slots[0].start == datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    assert all(a.end == b.start for a, b in zip(day.slots, day.slots[1:], strict=False))
    assert day.slots[0].value(False) == pytest.approx(0.281595)
    assert day.slots[0].value(True) == pytest.approx(0.346361)


def test_parse_tomorrow_incomplete() -> None:
    """Tomorrow's last hour is pending until the next publication."""
    day = parse_prices(load(f"prices_{TOMORROW}.json"), TARIFF)
    assert not day.complete and len(day.slots) == 96 and day.priced == 92
    assert day.pending_from == datetime(2026, 10, 26, 23, 0, tzinfo=UTC)
    assert "incomplete" in day.flags


def test_round_trip() -> None:
    """Cache serialisation keeps everything."""
    day = parse_prices(load(f"prices_{TOMORROW}.json"), TARIFF, fetched_at=datetime(2026, 10, 25, 13, tzinfo=UTC))
    again = DayPrices.from_dict(day.as_dict())
    assert again == day


def test_parse_tolerates_extra_fields() -> None:
    """New additive fields are kept or ignored, never break parsing."""
    body = load(f"prices_{TODAY}.json")
    body["new_top_level"] = {"x": 1}
    body["tariffs"][0].update({"valid_to": "2026-12-02", "source": "ficha", "unexpected": [1]})
    body["tariffs"][0]["prices"][0]["new_component"] = 0.1
    body["tariffs"][0]["prices"].append({"start_utc": None})
    body["version"] = "x"
    body["flags"] = None
    day = parse_prices(body, TARIFF)
    assert day.extra == {"valid_to": "2026-12-02", "source": "ficha"}
    assert day.version is None and day.flags == [] and len(day.slots) == 100


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda b: b.pop("date"), "date"),
        (lambda b: b.update(tariffs="nope"), "missing"),
        (lambda b: b.update(tariffs=[{"tariff": "other"}]), "missing"),
        (lambda b: b["tariffs"][0].update(available=False, reason="tri not offered"), "tri not offered"),
        (lambda b: b["tariffs"][0].update(available=False), "unknown reason"),
        (lambda b: b["tariffs"][0].update(prices=[]), "no price slots"),
    ],
)
def test_parse_errors(mutate, message: str) -> None:
    """Malformed responses raise LuzApiError."""
    body = load(f"prices_{TODAY}.json")
    mutate(body)
    with pytest.raises(LuzApiError, match=message):
        parse_prices(body, TARIFF)


def test_from_dict_bad_slot() -> None:
    """A corrupted cache is rejected."""
    raw = parse_prices(load(f"prices_{TODAY}.json"), TARIFF).as_dict()
    raw["slots"][0][0] = "garbage"
    with pytest.raises(ValueError, match="bad slot"):
        DayPrices.from_dict(raw)


def test_parse_utc() -> None:
    """ISO parsing helper."""
    assert parse_utc("2026-10-25T00:00:00+01:00") == datetime(2026, 10, 24, 23, tzinfo=UTC)
    assert parse_utc("2026-10-25T00:00:00") == datetime(2026, 10, 25, tzinfo=UTC)
    assert parse_utc("nope") is None
    assert parse_utc(None) is None


async def test_direct_headers_and_prices(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Direct mode sends a bearer key; RapidAPI quota headers are absent."""
    aioclient_mock.get(prices_url(TODAY), json=load(f"prices_{TODAY}.json"))
    calls = []
    client = LuzApiClient(
        async_get_clientsession(hass), MODE_DIRECT, "k", base_url=BASE + "/", on_call=lambda: calls.append(1)
    )
    day = await client.prices(date(2026, 10, 25), TARIFF, "simples")
    assert day.priced == 100 and calls == [1]
    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == "Bearer k"
    assert url.query["tariff"] == TARIFF and url.query["cycle"] == "simples"
    assert client.quota_remaining is None


async def test_rapidapi_headers_and_quota(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """RapidAPI mode sends the key + host and reads the quota headers."""
    aioclient_mock.get(
        f"https://{DEFAULT_RAPIDAPI_HOST}/v1/tariffs",
        json=load("tariffs.json"),
        headers={"x-ratelimit-requests-limit": "150", "x-ratelimit-requests-remaining": "149"},
    )
    client = LuzApiClient(async_get_clientsession(hass), MODE_RAPIDAPI, "k", rapidapi_host=DEFAULT_RAPIDAPI_HOST)
    tariffs = await client.tariffs()
    assert [t["id"] for t in tariffs][:2] == ["coopernico-base-2", "g9-smart-dynamic"]
    headers = aioclient_mock.mock_calls[0][3]
    assert headers["X-RapidAPI-Key"] == "k" and headers["X-RapidAPI-Host"] == DEFAULT_RAPIDAPI_HOST
    assert "Authorization" not in headers
    assert (client.quota_limit, client.quota_remaining) == (150, 149)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"status": 401, "json": {"detail": "Unauthorized"}}, LuzAuthError),
        ({"status": 403, "text": "You are not subscribed to this API."}, LuzAuthError),
        ({"status": 429, "json": {"message": "quota"}}, LuzRateLimitError),
        ({"status": 500, "text": "boom"}, LuzApiError),
        ({"status": 200, "text": "[1, 2]"}, LuzApiError),
        ({"status": 200, "text": "<html>"}, LuzApiError),
        ({"exc": aiohttp.ClientError("down")}, LuzConnectionError),
        ({"exc": TimeoutError()}, LuzConnectionError),
    ],
)
async def test_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, kwargs, error) -> None:
    """HTTP failures map to typed errors."""
    aioclient_mock.get(f"{BASE}/v1/tariffs", **kwargs)
    client = LuzApiClient(async_get_clientsession(hass), MODE_DIRECT, "k", base_url=BASE)
    with pytest.raises(error):
        await client.tariffs()


async def test_rate_limit_retry_after(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Retry-After is honoured; junk values are ignored."""
    client = LuzApiClient(async_get_clientsession(hass), MODE_DIRECT, "k", base_url=BASE)
    aioclient_mock.get(f"{BASE}/v1/tariffs", status=429, headers={"Retry-After": "120"})
    with pytest.raises(LuzRateLimitError) as err:
        await client.tariffs()
    assert err.value.retry_after == 120
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/v1/tariffs", status=429, headers={"Retry-After": "soon"})
    with pytest.raises(LuzRateLimitError) as err:
        await client.tariffs()
    assert err.value.retry_after is None


async def test_not_published(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """404 on /v1/prices carries the next publication time."""
    aioclient_mock.get(prices_url("2026-11-20"), status=404, json=load("not_published.json"))
    aioclient_mock.get(prices_url("2026-01-01"), status=404, text="not json")
    client = LuzApiClient(async_get_clientsession(hass), MODE_DIRECT, "k", base_url=BASE)
    with pytest.raises(LuzNotPublishedError) as err:
        await client.prices(date(2026, 11, 20), TARIFF, "simples")
    assert err.value.next_publication == datetime(2026, 11, 19, 12, 30, tzinfo=UTC)
    with pytest.raises(LuzNotPublishedError) as err:
        await client.prices(date(2026, 1, 1), TARIFF, "simples")
    assert err.value.next_publication is None


async def test_tariffs_empty(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """An empty catalogue is an error."""
    aioclient_mock.get(f"{BASE}/v1/tariffs", json={"tariffs": []})
    client = LuzApiClient(async_get_clientsession(hass), MODE_DIRECT, "k", base_url=BASE)
    with pytest.raises(LuzApiError):
        await client.tariffs()


async def test_bad_quota_header(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Non-numeric quota headers are ignored."""
    aioclient_mock.get(f"{BASE}/v1/tariffs", json=load("tariffs.json"), headers={"x-ratelimit-requests-remaining": "x"})
    client = LuzApiClient(async_get_clientsession(hass), MODE_DIRECT, "k", base_url=BASE)
    await client.tariffs()
    assert client.quota_remaining is None
