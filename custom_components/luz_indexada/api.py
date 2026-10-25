"""Small async client for the Luz Indexada API (RapidAPI or direct)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import aiohttp

from .const import MODE_RAPIDAPI, REQUEST_TIMEOUT, SLOT


class LuzError(Exception):
    """Base error."""


class LuzConnectionError(LuzError):
    """Network problem or timeout."""


class LuzAuthError(LuzError):
    """The key was rejected (401/403)."""


class LuzRateLimitError(LuzError):
    """Too many requests or quota exhausted (429)."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class LuzNotPublishedError(LuzError):
    """Prices for that day are not published (yet)."""

    def __init__(self, next_publication: datetime | None = None) -> None:
        super().__init__("not published yet")
        self.next_publication = next_publication


class LuzApiError(LuzError):
    """Unexpected response."""


def parse_utc(value: Any) -> datetime | None:
    """Parse an ISO-8601 instant ('Z' or offset) into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True, slots=True)
class Slot:
    """One quarter-hour price (EUR/kWh); None while OMIE hasn't published it."""

    start: datetime
    total: float | None
    total_vat: float | None
    period: str | None = None

    @property
    def end(self) -> datetime:
        """End of the quarter-hour."""
        return self.start + SLOT

    def value(self, include_vat: bool) -> float | None:
        """Price with or without 23% VAT."""
        return self.total_vat if include_vat else self.total


@dataclass(slots=True)
class DayPrices:
    """Prices of one tariff/cycle for one Lisbon day."""

    day: date
    complete: bool
    slots: list[Slot]
    status: str | None = None
    tariff_name: str | None = None
    flags: list[str] = field(default_factory=list)
    version: int | None = None
    pending_from: datetime | None = None
    formula: str | None = None
    ficha_date: str | None = None
    reference_kva: float | None = None
    periods_verified: bool | None = None
    fetched_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def priced(self) -> int:
        """Number of slots with a price."""
        return sum(1 for s in self.slots if s.total is not None)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the cache store."""
        return {
            "day": self.day.isoformat(),
            "complete": self.complete,
            "status": self.status,
            "tariff_name": self.tariff_name,
            "flags": list(self.flags),
            "version": self.version,
            "pending_from": self.pending_from.isoformat() if self.pending_from else None,
            "formula": self.formula,
            "ficha_date": self.ficha_date,
            "reference_kva": self.reference_kva,
            "periods_verified": self.periods_verified,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "extra": self.extra,
            "slots": [[s.start.isoformat(), s.total, s.total_vat, s.period] for s in self.slots],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DayPrices:
        """Inverse of as_dict."""
        slots = []
        for start, total, total_vat, period in raw["slots"]:
            parsed = parse_utc(start)
            if parsed is None:
                raise ValueError("bad slot start")
            slots.append(Slot(parsed, _num(total), _num(total_vat), period))
        return cls(
            day=date.fromisoformat(raw["day"]),
            complete=bool(raw["complete"]),
            slots=slots,
            status=raw.get("status"),
            tariff_name=raw.get("tariff_name"),
            flags=list(raw.get("flags") or []),
            version=raw.get("version"),
            pending_from=parse_utc(raw.get("pending_from")),
            formula=raw.get("formula"),
            ficha_date=raw.get("ficha_date"),
            reference_kva=_num(raw.get("reference_kva")),
            periods_verified=raw.get("periods_verified"),
            fetched_at=parse_utc(raw.get("fetched_at")),
            extra=dict(raw.get("extra") or {}),
        )


# Fields of a /v1/prices tariff entry kept as-is (additive API fields are tolerated and ignored otherwise).
_EXTRA_FIELDS = ("status_reason", "valid_from", "valid_to", "valid_until", "terms_expired", "source", "source_url")


def parse_prices(body: dict[str, Any], tariff: str, fetched_at: datetime | None = None) -> DayPrices:
    """Turn a /v1/prices response into DayPrices for one tariff."""
    try:
        day = date.fromisoformat(body["date"])
    except (KeyError, TypeError, ValueError) as err:
        raise LuzApiError("response has no valid 'date'") from err
    entries = body.get("tariffs")
    entry = (
        next(
            (t for t in entries if isinstance(t, dict) and t.get("tariff") == tariff),
            None,
        )
        if isinstance(entries, list)
        else None
    )
    if entry is None:
        raise LuzApiError(f"tariff {tariff!r} missing from the response")
    if entry.get("available") is False:
        raise LuzApiError(f"cycle not available: {entry.get('reason') or 'unknown reason'}")
    slots: list[Slot] = []
    for row in entry.get("prices") or []:
        start = parse_utc(row.get("start_utc")) if isinstance(row, dict) else None
        if start is None:
            continue
        slots.append(Slot(start, _num(row.get("total")), _num(row.get("total_vat23")), row.get("period")))
    if not slots:
        raise LuzApiError("response has no price slots")
    slots.sort(key=lambda s: s.start)
    flags = body.get("flags")
    return DayPrices(
        day=day,
        complete=bool(body.get("complete", True)),
        slots=slots,
        status=entry.get("status"),
        tariff_name=entry.get("name"),
        flags=[str(f) for f in flags] if isinstance(flags, list) else [],
        version=body.get("version") if isinstance(body.get("version"), int) else None,
        pending_from=parse_utc(body.get("pending_from_utc")),
        formula=entry.get("formula"),
        ficha_date=entry.get("ficha_date"),
        reference_kva=_num(entry.get("reference_kva")),
        periods_verified=entry.get("periods_verified"),
        fetched_at=fetched_at,
        extra={k: entry[k] for k in _EXTRA_FIELDS if k in entry},
    )


def _retry_after(value: str | None) -> float | None:
    try:
        return max(float(value), 0.0) if value is not None else None
    except ValueError:
        return None


def _int_header(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class LuzApiClient:
    """Talks to the API through RapidAPI (X-RapidAPI-Key) or directly (Bearer)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        mode: str,
        api_key: str,
        *,
        base_url: str | None = None,
        rapidapi_host: str | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self.on_call = on_call
        if mode == MODE_RAPIDAPI:
            host = (rapidapi_host or "").strip()
            self._base = f"https://{host}"
            self._headers = {"X-RapidAPI-Key": api_key, "X-RapidAPI-Host": host}
        else:
            self._base = (base_url or "").rstrip("/")
            self._headers = {"Authorization": f"Bearer {api_key}"}
        self._headers["Accept"] = "application/json"
        self.quota_limit: int | None = None
        self.quota_remaining: int | None = None

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.get(f"{self._base}{path}", params=params, headers=self._headers) as resp:
                    if self.on_call is not None:
                        self.on_call()
                    # RapidAPI reports the plan quota on every proxied response.
                    limit = _int_header(resp.headers.get("x-ratelimit-requests-limit"))
                    remaining = _int_header(resp.headers.get("x-ratelimit-requests-remaining"))
                    if remaining is not None:
                        self.quota_limit, self.quota_remaining = limit, remaining
                    status = resp.status
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:
                        body = None
        except (TimeoutError, aiohttp.ClientError) as err:
            raise LuzConnectionError(f"{type(err).__name__}: {err}") from err
        if status in (401, 403):
            raise LuzAuthError(f"HTTP {status}")
        if status == 429:
            raise LuzRateLimitError(_retry_after(retry_after))
        if status == 404 and path == "/v1/prices":
            nxt = body.get("next_publication_utc") if isinstance(body, dict) else None
            raise LuzNotPublishedError(parse_utc(nxt))
        if status >= 400:
            raise LuzApiError(f"HTTP {status}")
        if not isinstance(body, dict):
            raise LuzApiError("response is not a JSON object")
        return body

    async def tariffs(self) -> list[dict[str, Any]]:
        """Tariff catalogue."""
        body = await self._get("/v1/tariffs")
        tariffs = body.get("tariffs")
        if not isinstance(tariffs, list) or not tariffs:
            raise LuzApiError("no tariffs in the response")
        return [t for t in tariffs if isinstance(t, dict) and isinstance(t.get("id"), str)]

    async def prices(self, day: date, tariff: str, cycle: str, now: datetime | None = None) -> DayPrices:
        """Prices for one Lisbon day."""
        body = await self._get("/v1/prices", {"date": day.isoformat(), "tariff": tariff, "cycle": cycle})
        return parse_prices(body, tariff, fetched_at=now)
