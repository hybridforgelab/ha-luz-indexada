"""Coordinator: frugal fetch schedule + local quarter-hour updates."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DayPrices,
    LuzApiClient,
    LuzAuthError,
    LuzError,
    LuzNotPublishedError,
    LuzRateLimitError,
)
from .const import (
    CONF_CYCLE,
    CONF_INCLUDE_VAT,
    CONF_TARIFF,
    CONF_WINDOW_HOURS,
    DEFAULT_CYCLE,
    DEFAULT_INCLUDE_VAT,
    DEFAULT_WINDOW_HOURS,
    DOMAIN,
    ERROR_BACKOFF_MAX,
    ERROR_BACKOFF_MIN,
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
    """

    config_entry: LuzConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LuzConfigEntry, client: LuzApiClient) -> None:
        super().__init__(hass, LOGGER, name=DOMAIN, config_entry=entry, update_interval=None)
        self.client = client
        self.tariff: str = entry.data[CONF_TARIFF]
        self.cycle: str = entry.data.get(CONF_CYCLE, DEFAULT_CYCLE)
        self.today: DayPrices | None = None
        self.tomorrow: DayPrices | None = None
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
        calls = raw.get("calls") or {}
        self.calls_month = calls.get("month")
        self.calls_month_count = int(calls.get("month_count") or 0)
        self.calls_total = int(calls.get("total") or 0)

    def _cache(self) -> dict[str, Any]:
        return {
            "tariff": self.tariff,
            "cycle": self.cycle,
            "today": self.today.as_dict() if self.today else None,
            "tomorrow": self.tomorrow.as_dict() if self.tomorrow else None,
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

    async def _fetch(self, day: date, now: datetime) -> DayPrices:
        prices = await self.client.prices(day, self.tariff, self.cycle, now=now)
        if prices.day != day:
            raise LuzNotPublishedError(None)
        self._dirty = True
        return prices

    async def _async_update_data(self) -> PriceState:
        now = dt_util.utcnow()
        local = now.astimezone(LISBON)
        today, tomorrow = local.date(), local.date() + timedelta(days=1)

        # Midnight: yesterday's "tomorrow" becomes today (no API call).
        if self.today is not None and self.today.day != today:
            self.today, self._dirty = None, True
        if self.today is None and self.tomorrow is not None and self.tomorrow.day == today:
            self.today, self.tomorrow = self.tomorrow, None
            self.today_next_attempt = None
        if self.tomorrow is not None and self.tomorrow.day != tomorrow:
            self.tomorrow, self._dirty = None, True

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
        return build_state(self.today, self.tomorrow, now, self.include_vat, self.window_hours)

    async def _fetch_needed(self, now: datetime, local: datetime, today: date, tomorrow: date) -> None:
        if self.today is None:
            try:
                self.today = await self._fetch(today, now)
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
                self.tomorrow = await self._fetch(tomorrow, now)
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
                refreshed = await self._fetch(today, now)
            except LuzNotPublishedError:
                return
            if refreshed.priced >= self.today.priced:
                self.today = refreshed
            if self.today.complete:
                self.today_next_attempt = None
