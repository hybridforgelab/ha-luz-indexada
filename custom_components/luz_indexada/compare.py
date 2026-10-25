"""Supplier comparison, computed locally from cached responses (no API calls)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .api import CompareResult, DayPrices, TariffCost, is_retired


@dataclass(frozen=True, slots=True)
class TariffCurve:
    """Today's and tomorrow's prices of one comparable tariff."""

    tariff: str
    name: str
    status: str | None
    today: DayPrices | None
    tomorrow: DayPrices | None
    today_avg: float | None
    today_min: float | None
    today_max: float | None


@dataclass(frozen=True, slots=True)
class Comparison:
    """Comparable tariffs (withdrawn and unavailable ones dropped), their curves and monthly costs."""

    selected: str
    include_vat: bool
    curves: dict[str, TariffCurve] = field(default_factory=dict)
    costs: dict[str, TariffCost] = field(default_factory=dict)
    selected_cost: TariffCost | None = None
    compare: CompareResult | None = None

    def cost(self, tariff: str) -> float | None:
        """Monthly cost of a comparable tariff (VAT per the option)."""
        c = self.costs.get(tariff)
        return c.value(self.include_vat) if c else None

    def saving(self, tariff: str) -> float | None:
        """What switching from the selected tariff to `tariff` saves per month (negative: costs more)."""
        cost = self.cost(tariff)
        if cost is None or self.selected_cost is None:
            return None
        return round(self.selected_cost.value(self.include_vat) - cost, 2)

    @property
    def ranking(self) -> list[TariffCost]:
        """Comparable tariffs, cheapest first."""
        return sorted(self.costs.values(), key=lambda c: (c.value(self.include_vat), c.tariff))

    @property
    def cheapest(self) -> TariffCost | None:
        """Cheapest comparable tariff."""
        ranking = self.ranking
        return ranking[0] if ranking else None

    def summary(self) -> dict[str, Any]:
        """Attributes of the cheapest-supplier sensor."""
        ranking = self.ranking
        cheapest = ranking[0] if ranking else None
        exact = next((c for c in ranking if c.status != "estimate"), None)
        cmp = self.compare
        return {
            "selected_tariff": self.selected,
            "selected_cost": self.selected_cost.value(self.include_vat) if self.selected_cost else None,
            "cheapest_tariff": cheapest.tariff if cheapest else None,
            "cheapest_cost": cheapest.value(self.include_vat) if cheapest else None,
            "saving": self.saving(cheapest.tariff) if cheapest else None,
            "cheapest_exact": exact.name if exact else None,
            "tariffs": [
                {
                    "tariff": c.tariff,
                    "name": c.name,
                    "cost": c.value(self.include_vat),
                    "saving": self.saving(c.tariff),
                    "status": c.status,
                    "estimate": c.status == "estimate",
                    "selected": c.tariff == self.selected,
                }
                for c in ranking
            ],
            "monthly_kwh": cmp.kwh if cmp else None,
            "kva": cmp.kva if cmp else None,
            "month": cmp.month if cmp else None,
            "vat_included": self.include_vat,
            "notes": cmp.notes if cmp else [],
        }


def _stats(day: DayPrices | None, include_vat: bool) -> tuple[float | None, float | None, float | None]:
    values = [v for s in day.slots if (v := s.value(include_vat)) is not None] if day else []
    if not values:
        return None, None, None
    return round(sum(values) / len(values), 6), min(values), max(values)


def build_comparison(
    *,
    selected: str,
    today: date,
    peers_today: dict[str, DayPrices],
    peers_tomorrow: dict[str, DayPrices],
    compare: CompareResult | None,
    catalogue: dict[str, dict[str, Any]],
    include_vat: bool,
) -> Comparison:
    """Drop withdrawn tariffs (catalogue, or the entry's own fields) and build curves and costs."""
    iso = today.isoformat()

    def comparable(tariff: str, own: dict[str, Any]) -> bool:
        return not is_retired(catalogue.get(tariff) or {}, iso) and not is_retired(own, iso)

    curves: dict[str, TariffCurve] = {}
    for tariff, day in peers_today.items():
        if not comparable(tariff, day.extra):
            continue
        avg, lo, hi = _stats(day, include_vat)
        # The catalogue and the price entry can disagree; "estimate" wins so it is never hidden.
        status = "estimate" if "estimate" in (day.status, (catalogue.get(tariff) or {}).get("status")) else day.status
        curves[tariff] = TariffCurve(
            tariff=tariff,
            name=day.tariff_name or tariff,
            status=status,
            today=day,
            tomorrow=peers_tomorrow.get(tariff),
            today_avg=avg,
            today_min=lo,
            today_max=hi,
        )
    costs = {t: c for t, c in (compare.costs if compare else {}).items() if comparable(t, {})}
    return Comparison(
        selected=selected,
        include_vat=include_vat,
        curves=curves,
        costs=costs,
        selected_cost=compare.costs.get(selected) if compare else None,
        compare=compare,
    )
