"""Local computations (current slot, stats, cheapest window)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.luz_indexada.api import DayPrices, Slot, parse_prices
from custom_components.luz_indexada.prices import build_state, cheapest_window

from .conftest import TARIFF, TODAY, TOMORROW, load

T0 = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)


def _day(values: list[float | None], start: datetime = T0) -> DayPrices:
    slots = [
        Slot(start + timedelta(minutes=15 * i), v, None if v is None else round(v * 1.23, 6), "simples")
        for i, v in enumerate(values)
    ]
    return DayPrices(day=start.date(), complete=None not in values, slots=slots, status="exact")


def test_state_on_real_day() -> None:
    """Current/next slot, stats and window on the DST day."""
    today = parse_prices(load(f"prices_{TODAY}.json"), TARIFF)
    tomorrow = parse_prices(load(f"prices_{TOMORROW}.json"), TARIFF)
    now = datetime(2026, 10, 25, 18, 7, tzinfo=UTC)
    s = build_state(today, tomorrow, now, include_vat=False, window_hours=2)
    assert s.current is not None and s.current.start == datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
    assert s.next is not None and s.next.start == datetime(2026, 10, 25, 18, 15, tzinfo=UTC)
    assert s.today_min <= s.today_avg <= s.today_max
    assert s.window is not None and s.window.end - s.window.start == timedelta(hours=2)
    assert s.window.start >= s.current.start
    assert s.tomorrow_available
    assert len(s.raw(today)) == 100 and s.raw(today)[0]["start"] == "2026-10-25T00:00:00+01:00"
    assert s.raw(None) == [] and s.values(None) == []
    vat = build_state(today, tomorrow, now, include_vat=True, window_hours=2)
    assert vat.current_price == pytest.approx(s.current_price * 1.23, abs=1e-5)


def test_next_slot_crosses_midnight() -> None:
    """At 23:45 the next price comes from tomorrow; unknown without it."""
    today = parse_prices(load(f"prices_{TODAY}.json"), TARIFF)
    tomorrow = parse_prices(load(f"prices_{TOMORROW}.json"), TARIFF)
    now = datetime(2026, 10, 25, 23, 50, tzinfo=UTC)
    assert build_state(today, tomorrow, now, False, 1).next_price == pytest.approx(0.340947)
    assert build_state(today, None, now, False, 1).next_price is None


def test_cheapest_window_skips_gaps() -> None:
    """Pending (None) slots and non-contiguous slots break a window."""
    values = [0.3, 0.1, None, 0.05, 0.06, 0.2, 0.01]
    w = cheapest_window(_day(values).slots, False, T0, 0.5)
    assert w is not None and w.start == T0 + timedelta(minutes=45) and w.average == pytest.approx(0.055)
    assert cheapest_window(_day(values).slots, False, T0, 3) is None
    gap = _day([0.1, 0.1]).slots + _day([0.0, 0.0], T0 + timedelta(hours=5)).slots
    assert cheapest_window(gap, False, T0, 0.75) is None
    assert cheapest_window(gap, False, T0, 0.5).start == T0 + timedelta(hours=5)


def test_cheapest_window_starts_from_current_slot() -> None:
    """Past slots are ignored; the slot in progress counts."""
    day = _day([0.01, 0.5, 0.4, 0.3])
    now = T0 + timedelta(minutes=20)
    w = cheapest_window(day.slots, False, now, 0.25)
    assert w.start == T0 + timedelta(minutes=45)


def test_cheap_now() -> None:
    """Below average or inside the window."""
    day = _day([0.3, 0.1, 0.1, 0.3, 0.3, 0.3])
    assert build_state(day, None, T0 + timedelta(minutes=16), False, 0.5).cheap_now is True
    assert build_state(day, None, T0, False, 0.5).cheap_now is False
    # Above today's average, but the cheapest block still ahead starts now
    tail = _day([0.01, 0.01, 0.5, 0.5, 0.6, 0.6])
    s = build_state(tail, None, T0 + timedelta(minutes=31), False, 0.5)
    assert s.current_price > s.today_avg and s.cheap_now is True


def test_state_without_prices() -> None:
    """A day with only pending slots, or a time outside the day."""
    day = _day([None, None])
    s = build_state(day, None, T0, False, 1)
    assert s.current_price is None and s.today_avg is None and s.window is None
    assert s.cheap_now is None and not s.tomorrow_available
    outside = build_state(_day([0.1]), None, T0 + timedelta(days=2), False, 1)
    assert outside.current is None and outside.next is None and outside.cheap_now is None
    assert outside.today.day == date(2026, 10, 26)
