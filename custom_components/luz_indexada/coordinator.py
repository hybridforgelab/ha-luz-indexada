"""Coordinator: frugal fetch schedule + local quarter-hour updates."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CompareResult,
    DayPrices,
    LuzApiClient,
    LuzAuthError,
    LuzError,
    LuzNotPublishedError,
    LuzRateLimitError,
    parse_utc,
)
from .compare import build_comparison
from .const import (
    CATALOGUE_MAX_AGE,
    COMPARE_RETRY,
    CONF_COMPARE,
    CONF_CYCLE,
    CONF_INCLUDE_VAT,
    CONF_KVA,
    CONF_MONTHLY_KWH,
    CONF_TARIFF,
    CONF_WINDOW_HOURS,
    DEFAULT_COMPARE,
    DEFAULT_CYCLE,
    DEFAULT_INCLUDE_VAT,
    DEFAULT_KVA,
    DEFAULT_MONTHLY_KWH,
    DEFAULT_WINDOW_HOURS,
    DOMAIN,
    ERROR_BACKOFF_MAX,
    ERROR_BACKOFF_MIN,
    KVA_STEPS,
    LISBON,
    LOGGER,
    PUBLICATION_HOUR,
    RATE_LIMIT_BACKOFF,
    RATE_LIMIT_BACKOFF_MAX,
    STORAGE_VERSION,
    TOMORROW_RETRY,
)
from .prices import PriceState, build_state

type LuzConfigEntry = ConfigEntry[LuzCoordinator]


class LuzCoordinator(DataUpdateCoordinator[PriceState]):
    """Holds today's/tomorrow's slots and decides when an API call is really needed.

    Refreshed on every quarter-hour by a time tracker; most refreshes only recompute
    the state from the cache. Network calls happen only when:
    * no usable prices for today are cached (first install, or tomorrow was never fetched);
    * it is after PUBLICATION_HOUR Lisbon and tomorrow is not cached (retried hourly, or
      at the API's announced next publication time);
    * tomorrow is out and today's last hour is still pending (filled by the same publication).

    With the supplier comparison on, those same /v1/prices calls drop the tariff filter and return every
    tariff at once (no extra calls); /v1/compare is called once a Lisbon day (or when the consumption or the
    power option changes) and the tariff catalogue once a week, to leave withdrawn tariffs out.
    """

    config_entry: LuzConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LuzConfigEntry, client: LuzApiClient) -> None:
        super().__init__(hass, LOGGER, name=DOMAIN, config_entry=entry, update_interval=None)
        self.client = client
        self.tariff: str = entry.data[CONF_TARIFF]
        self.cycle: str = entry.data.get(CONF_CYCLE, DEFAULT_CYCLE)
        self.today: DayPrices | None = None
        self.tomorrow: DayPrices | None = None
        # Every tariff of the same day (comparison on), the selected one included.
        self.peers_today: dict[str, DayPrices] = {}
        self.peers_tomorrow: dict[str, DayPrices] = {}
        self.compare_result: CompareResult | None = None
        self.compare_next_attempt: datetime | None = None
        self.compare_error: str | None = None
        self.catalogue: dict[str, dict[str, Any]] = {}
        self.catalogue_fetched: datetime | None = None
        self.tomorrow_next_attempt: datetime | None = None
        self.today_next_attempt: datetime | None = None
        self.backoff_until: datetime | None = None
        self._error_backoff = ERROR_BACKOFF_MIN
        self._rate_backoff = RATE_LIMIT_BACKOFF
        self.last_error: str | None = None
        self.calls_month: str | None = None
        self.calls_month_count = 0
        self.calls_total = 0
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._dirty = False

    @property
    def include_vat(self) -> bool:
        """VAT option."""
        return bool(self.config_entry.options.get(CONF_INCLUDE_VAT, DEFAULT_INCLUDE_VAT))

    @property
    def window_hours(self) -> float:
        """Cheapest window length option."""
        return float(self.config_entry.options.get(CONF_WINDOW_HOURS, DEFAULT_WINDOW_HOURS))

    @property
    def compare(self) -> bool:
        """Supplier comparison option (on by default, also for entries created before it existed)."""
        return bool(self.config_entry.options.get(CONF_COMPARE, DEFAULT_COMPARE))

    @property
    def monthly_kwh(self) -> float:
        """Monthly consumption used for the cost comparison."""
        return float(self.config_entry.options.get(CONF_MONTHLY_KWH, DEFAULT_MONTHLY_KWH))

    @property
    def kva(self) -> str:
        """Contracted power used for the cost comparison (a BTN step)."""
        kva = str(self.config_entry.options.get(CONF_KVA, DEFAULT_KVA))
        return kva if kva in KVA_STEPS else DEFAULT_KVA

    # --- cache -------------------------------------------------------------------------------

    async def async_load_cache(self) -> None:
        """Restore cached prices so a restart doesn't spend API calls."""
        raw = await self._store.async_load()
        if not raw or raw.get("tariff") != self.tariff or raw.get("cycle") != self.cycle:
            return
        try:
            self.today = DayPrices.from_dict(raw["today"]) if raw.get("today") else None
            self.tomorrow = DayPrices.from_dict(raw["tomorrow"]) if raw.get("tomorrow") else None
        except (KeyError, TypeError, ValueError):
            LOGGER.debug("Ignoring an unreadable price cache")
            self.today = self.tomorrow = None
        if self.compare:
            self._load_comparison_cache(raw)
        calls = raw.get("calls") or {}
        self.calls_month = calls.get("month")
        self.calls_month_count = int(calls.get("month_count") or 0)
        self.calls_total = int(calls.get("total") or 0)

    def _load_comparison_cache(self, raw: dict[str, Any]) -> None:
        try:
            self.peers_today = {t: DayPrices.from_dict(d) for t, d in (raw.get("peers_today") or {}).items()}
            self.peers_tomorrow = {t: DayPrices.from_dict(d) for t, d in (raw.get("peers_tomorrow") or {}).items()}
            self.compare_result = CompareResult.from_dict(raw["compare"]) if raw.get("compare") else None
            catalogue = raw.get("catalogue") or {}
            self.catalogue = dict(catalogue.get("tariffs") or {})
            self.catalogue_fetched = parse_utc(catalogue.get("fetched_at"))
        except (KeyError, TypeError, ValueError, AttributeError, LuzError):
            LOGGER.debug("Ignoring an unreadable comparison cache")
            self.peers_today, self.peers_tomorrow, self.compare_result = {}, {}, None
            self.catalogue, self.catalogue_fetched = {}, None

    def _cache(self) -> dict[str, Any]:
        return {
            "tariff": self.tariff,
            "cycle": self.cycle,
            "today": self.today.as_dict() if self.today else None,
            "tomorrow": self.tomorrow.as_dict() if self.tomorrow else None,
            "peers_today": {t: d.as_dict() for t, d in self.peers_today.items()},
            "peers_tomorrow": {t: d.as_dict() for t, d in self.peers_tomorrow.items()},
            "compare": self.compare_result.as_dict() if self.compare_result else None,
            "catalogue": {
                "fetched_at": self.catalogue_fetched.isoformat() if self.catalogue_fetched else None,
                "tariffs": self.catalogue,
            },
            "calls": {"month": self.calls_month, "month_count": self.calls_month_count, "total": self.calls_total},
        }

    def count_call(self) -> None:
        """Called by the client for every HTTP response received."""
        month = dt_util.utcnow().astimezone(LISBON).strftime("%Y-%m")
        if month != self.calls_month:
            self.calls_month, self.calls_month_count = month, 0
        self.calls_month_count += 1
        self.calls_total += 1
        self._dirty = True

    # --- schedule ----------------------------------------------------------------------------

    async def _fetch(self, day: date, now: datetime) -> tuple[DayPrices, dict[str, DayPrices]]:
        """The selected tariff's day, plus every tariff of that day when the comparison is on (same call)."""
        if self.compare:
            prices, peers = await self.client.prices_all(day, self.tariff, self.cycle, now=now)
        else:
            prices, peers = await self.client.prices(day, self.tariff, self.cycle, now=now), {}
        if prices.day != day:
            raise LuzNotPublishedError(None)
        self._dirty = True
        return prices, peers

    async def _async_update_data(self) -> PriceState:
        now = dt_util.utcnow()
        local = now.astimezone(LISBON)
        today, tomorrow = local.date(), local.date() + timedelta(days=1)

        # Midnight: yesterday's "tomorrow" becomes today (no API call).
        if self.today is not None and self.today.day != today:
            self.today, self._dirty = None, True
        if self.today is None and self.tomorrow is not None and self.tomorrow.day == today:
            self.today, self.tomorrow = self.tomorrow, None
            self.peers_today, self.peers_tomorrow = self.peers_tomorrow, {}
            self.today_next_attempt = None
        if self.tomorrow is not None and self.tomorrow.day != tomorrow:
            self.tomorrow, self._dirty = None, True
        if self.today is None:
            self.peers_today = {}
        if self.tomorrow is None:
            self.peers_tomorrow = {}

        if self.backoff_until is None or now >= self.backoff_until:
            try:
                await self._fetch_needed(now, local, today, tomorrow)
            except LuzAuthError as err:
                raise ConfigEntryAuthFailed("the API key was rejected") from err
            except LuzRateLimitError as err:
                wait = timedelta(seconds=err.retry_after) if err.retry_after else self._rate_backoff
                self.backoff_until = now + max(wait, RATE_LIMIT_BACKOFF)
                self._rate_backoff = min(self._rate_backoff * 2, RATE_LIMIT_BACKOFF_MAX)
                self.last_error = "rate limited (429)"
                LOGGER.warning("Luz Indexada API rate limit reached; next attempt at %s", self.backoff_until)
            except LuzError as err:
                self.backoff_until = now + self._error_backoff
                self._error_backoff = min(self._error_backoff * 2, ERROR_BACKOFF_MAX)
                self.last_error = str(err)
                LOGGER.debug("Luz Indexada API error (%s); next attempt at %s", err, self.backoff_until)
            else:
                self.backoff_until = None
                self._error_backoff = ERROR_BACKOFF_MIN
                self._rate_backoff = RATE_LIMIT_BACKOFF

        if self._dirty:
            self._dirty = False
            await self._store.async_save(self._cache())

        if self.today is None:
            raise UpdateFailed(f"no prices for {today} yet ({self.last_error or 'not published'})")
        state = build_state(self.today, self.tomorrow, now, self.include_vat, self.window_hours)
        if not self.compare:
            return state
        comparison = build_comparison(
            selected=self.tariff,
            today=today,
            peers_today=self.peers_today,
            peers_tomorrow=self.peers_tomorrow,
            compare=self.compare_result,
            catalogue=self.catalogue,
            include_vat=self.include_vat,
        )
        return replace(state, comparison=comparison)

    async def _fetch_needed(self, now: datetime, local: datetime, today: date, tomorrow: date) -> None:
        if self.today is None:
            try:
                self.today, self.peers_today = await self._fetch(today, now)
            except LuzNotPublishedError:
                self.last_error = f"prices for {today} not available"
                raise
            self.last_error = None

        if (
            local.hour >= PUBLICATION_HOUR
            and self.tomorrow is None
            and (self.tomorrow_next_attempt is None or now >= self.tomorrow_next_attempt)
        ):
            try:
                self.tomorrow, self.peers_tomorrow = await self._fetch(tomorrow, now)
            except LuzNotPublishedError as err:
                retry = now + TOMORROW_RETRY
                if err.next_publication is not None and err.next_publication > retry:
                    retry = err.next_publication
                self.tomorrow_next_attempt = retry
                LOGGER.debug("Tomorrow's prices not published yet; next attempt at %s", retry)
            else:
                self.tomorrow_next_attempt = None
                self.last_error = None

        # Today's last Lisbon hour is published together with tomorrow.
        if (
            self.tomorrow is not None
            and self.today is not None
            and not self.today.complete
            and (self.today_next_attempt is None or now >= self.today_next_attempt)
        ):
            self.today_next_attempt = now + TOMORROW_RETRY
            try:
                refreshed, peers = await self._fetch(today, now)
            except LuzNotPublishedError:
                refreshed = None
            if refreshed is not None and refreshed.priced >= self.today.priced:
                self.today, self.peers_today = refreshed, peers
            if self.today.complete:
                self.today_next_attempt = None

        # Comparison just turned on (or an older cache): fill the other tariffs once per cached day.
        if self.compare and self.today is not None and not self.peers_today:
            refreshed, self.peers_today = await self._fetch(today, now)
            if refreshed.priced >= self.today.priced:
                self.today = refreshed
        if self.compare and self.tomorrow is not None and not self.peers_tomorrow:
            self.tomorrow, self.peers_tomorrow = await self._fetch(tomorrow, now)

        await self._compare_needed(now, today)

    async def _compare_needed(self, now: datetime, today: date) -> None:
        """Monthly costs: once a Lisbon day, or when the options change. Errors here never block the prices."""
        if not self.compare:
            return
        cached = self.compare_result
        if (
            cached is not None
            and cached.fetched_day == today.isoformat()
            and cached.kwh == self.monthly_kwh
            and cached.kva == float(self.kva)
        ):
            return
        if self.compare_next_attempt is not None and now < self.compare_next_attempt:
            return
        try:
            if self.catalogue_fetched is None or now - self.catalogue_fetched >= CATALOGUE_MAX_AGE:
                tariffs = await self.client.tariffs()
                self.catalogue = {
                    t["id"]: {k: t.get(k) for k in ("name", "status", "replaced_by", "valid_until")} for t in tariffs
                }
                self.catalogue_fetched, self._dirty = now, True
            result = await self.client.compare(self.monthly_kwh, self.kva, self.cycle, now=now)
        except (LuzAuthError, LuzRateLimitError):
            raise
        except LuzError as err:
            self.compare_next_attempt = now + COMPARE_RETRY
            self.compare_error = str(err)
            LOGGER.debug("Supplier comparison failed (%s); next attempt at %s", err, self.compare_next_attempt)
            return
        # The cache key is what was asked for, so a response that echoes other values can't cause a refetch loop.
        result.fetched_day, result.kwh, result.kva = today.isoformat(), self.monthly_kwh, float(self.kva)
        self.compare_result, self.compare_next_attempt, self.compare_error = result, None, None
        self._dirty = True
