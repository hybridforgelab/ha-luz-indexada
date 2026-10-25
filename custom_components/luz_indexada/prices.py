"""Local computations on cached slots (no API calls)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .api import DayPrices, Slot
from .const import LISBON


@dataclass(frozen=True, slots=True)
class Window:
    """Cheapest contiguous block."""

    start: datetime
    end: datetime
    average: float


@dataclass(frozen=True, slots=True)
class PriceState:
    """Everything the entities show, derived from cached days at a given instant."""

    today: DayPrices
    tomorrow: DayPrices | None
    include_vat: bool
    window_hours: float
    current: Slot | None
    next: Slot | None
    current_price: float | None
    next_price: float | None
    today_min: float | None
    today_max: float | None
    today_avg: float | None
    today_min_at: datetime | None
    today_max_at: datetime | None
    window: Window | None

    @property
    def tomorrow_available(self) -> bool:
        """True once tomorrow's prices are cached."""
        return self.tomorrow is not None and self.tomorrow.priced > 0

    @property
    def cheap_now(self) -> bool | None:
        """Below today's average, or inside the cheapest window."""
        if self.current is None or self.current_price is None:
            return None
        in_window = self.window is not None and self.window.start <= self.current.start < self.window.end
        below_avg = self.today_avg is not None and self.current_price < self.today_avg
        return in_window or below_avg

    def raw(self, day: DayPrices | None) -> list[dict[str, str | float | None]]:
        """Slots as [{start, end, value}] in local time (Nord Pool / ENTSO-e style, ApexCharts friendly)."""
        if day is None:
            return []
        return [
            {
                "start": s.start.astimezone(LISBON).isoformat(),
                "end": s.end.astimezone(LISBON).isoformat(),
                "value": s.value(self.include_vat),
            }
            for s in day.slots
        ]

    def values(self, day: DayPrices | None) -> list[float | None]:
        """Plain list of prices for the day."""
        return [] if day is None else [s.value(self.include_vat) for s in day.slots]


def cheapest_window(slots: list[Slot], include_vat: bool, now: datetime, hours: float) -> Window | None:
    """Cheapest run of contiguous priced slots of `hours`, starting at the current slot or later."""
    size = max(1, round(hours * 4))
    candidates = [s for s in slots if s.end > now]
    best: Window | None = None
    run: list[tuple[Slot, float]] = []
    total = 0.0
    for slot in candidates:
        price = slot.value(include_vat)
        if price is None or (run and run[-1][0].end != slot.start):
            run, total = [], 0.0
        if price is None:
            continue
        run.append((slot, price))
        total += price
        if len(run) > size:
            total -= run.pop(0)[1]
        if len(run) == size and (best is None or total / size < best.average - 1e-12):
            best = Window(run[0][0].start, slot.end, total / size)
    return best


def build_state(
    today: DayPrices,
    tomorrow: DayPrices | None,
    now: datetime,
    include_vat: bool,
    window_hours: float,
) -> PriceState:
    """Derive the current state from cached days."""
    slots = list(today.slots) + (list(tomorrow.slots) if tomorrow else [])
    current = next((s for s in slots if s.start <= now < s.end), None)
    nxt = next((s for s in slots if s.start == current.end), None) if current else None
    priced = [(s, v) for s in today.slots if (v := s.value(include_vat)) is not None]
    lo = min(priced, key=lambda p: p[1]) if priced else None
    hi = max(priced, key=lambda p: p[1]) if priced else None
    avg = sum(v for _, v in priced) / len(priced) if priced else None
    return PriceState(
        today=today,
        tomorrow=tomorrow,
        include_vat=include_vat,
        window_hours=window_hours,
        current=current,
        next=nxt,
        current_price=current.value(include_vat) if current else None,
        next_price=nxt.value(include_vat) if nxt else None,
        today_min=lo[1] if lo else None,
        today_max=hi[1] if hi else None,
        today_avg=round(avg, 6) if avg is not None else None,
        today_min_at=lo[0].start if lo else None,
        today_max_at=hi[0].start if hi else None,
        window=cheapest_window(slots, include_vat, now, window_hours),
    )
