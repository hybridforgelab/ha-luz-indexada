"""Fetch live prices with the integration's own client and print what the entities would show.

Direct mode (self-hosted / testing):
    LUZ_BASE_URL=https://api.example LUZ_API_KEY=... python scripts/verify_live.py
RapidAPI mode:
    LUZ_RAPIDAPI_KEY=... python scripts/verify_live.py [--host <slug>.p.rapidapi.com]

Makes 1-2 requests (today, plus tomorrow after 13:00 Lisbon), plus 2 for the supplier comparison
(/v1/tariffs and /v1/compare; skip with --no-compare). Never prints the key.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sys

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.luz_indexada.api import DayPrices, LuzApiClient, LuzNotPublishedError
from custom_components.luz_indexada.compare import Comparison, build_comparison
from custom_components.luz_indexada.const import (
    DEFAULT_KVA,
    DEFAULT_MONTHLY_KWH,
    DEFAULT_RAPIDAPI_HOST,
    KVA_STEPS,
    LISBON,
    MODE_DIRECT,
    MODE_RAPIDAPI,
    PUBLICATION_HOUR,
)
from custom_components.luz_indexada.prices import build_state


def _t(value: datetime | None) -> str:
    return value.astimezone(LISBON).strftime("%Y-%m-%d %H:%M %Z") if value else "-"


def _p(value: float | None) -> str:
    return f"{value:.4f} EUR/kWh" if value is not None else "-"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tariff", default="galp-plano-flexivel")
    ap.add_argument("--cycle", default="simples")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--no-vat", action="store_true")
    ap.add_argument("--host", default=DEFAULT_RAPIDAPI_HOST)
    ap.add_argument("--no-compare", action="store_true", help="only the selected tariff (fewer requests)")
    ap.add_argument("--kwh", type=float, default=DEFAULT_MONTHLY_KWH, help="monthly consumption for /v1/compare")
    ap.add_argument("--kva", default=DEFAULT_KVA, choices=KVA_STEPS)
    args = ap.parse_args()

    calls = 0

    def count() -> None:
        nonlocal calls
        calls += 1

    async with aiohttp.ClientSession() as session:
        if key := os.getenv("LUZ_RAPIDAPI_KEY"):
            client = LuzApiClient(session, MODE_RAPIDAPI, key, rapidapi_host=args.host, on_call=count)
            mode = f"rapidapi ({args.host})"
        else:
            key, base = os.getenv("LUZ_API_KEY"), os.getenv("LUZ_BASE_URL")
            if not key or not base:
                print("set LUZ_RAPIDAPI_KEY, or LUZ_BASE_URL + LUZ_API_KEY", file=sys.stderr)
                return 2
            client = LuzApiClient(session, MODE_DIRECT, key, base_url=base, on_call=count)
            mode = f"direct ({base})"

        now = datetime.now(UTC)
        local = now.astimezone(LISBON)
        compare = not args.no_compare
        peers_today: dict[str, DayPrices] = {}
        peers_tomorrow: dict[str, DayPrices] = {}
        if compare:
            today, peers_today = await client.prices_all(local.date(), args.tariff, args.cycle, now=now)
        else:
            today = await client.prices(local.date(), args.tariff, args.cycle, now=now)
        tomorrow = None
        if local.hour >= PUBLICATION_HOUR:
            day = local.date() + timedelta(days=1)
            try:
                if compare:
                    tomorrow, peers_tomorrow = await client.prices_all(day, args.tariff, args.cycle, now=now)
                else:
                    tomorrow = await client.prices(day, args.tariff, args.cycle, now=now)
            except LuzNotPublishedError as err:
                print(f"tomorrow: not published yet (next publication {_t(err.next_publication)})")
        comparison = None
        if compare:
            catalogue = {t["id"]: t for t in await client.tariffs()}
            result = await client.compare(args.kwh, args.kva, args.cycle, now=now)
            comparison = build_comparison(
                selected=args.tariff,
                today=local.date(),
                peers_today=peers_today,
                peers_tomorrow=peers_tomorrow,
                compare=result,
                catalogue=catalogue,
                include_vat=not args.no_vat,
            )

    s = build_state(today, tomorrow, now, include_vat=not args.no_vat, window_hours=args.hours)
    print(f"mode: {mode}; API calls: {calls}; now: {_t(now)}")
    print(
        f"tariff: {today.tariff_name} [{args.tariff} / {args.cycle}] status={today.status} estimate={today.status == 'estimate'}"
    )
    print(
        f"today {today.day}: {len(today.slots)} slots, {today.priced} priced, complete={today.complete}, flags={today.flags}"
    )
    if tomorrow:
        print(
            f"tomorrow {tomorrow.day}: {len(tomorrow.slots)} slots, {tomorrow.priced} priced, complete={tomorrow.complete}"
        )
    print(f"VAT included: {s.include_vat}")
    print(f"sensor current_price:          {_p(s.current_price)} (period {s.current.period if s.current else '-'})")
    print(f"sensor next_price:             {_p(s.next_price)} at {_t(s.next.start if s.next else None)}")
    print(f"sensor today_min:              {_p(s.today_min)} at {_t(s.today_min_at)}")
    print(f"sensor today_max:              {_p(s.today_max)} at {_t(s.today_max_at)}")
    print(f"sensor today_avg:              {_p(s.today_avg)}")
    w = s.window
    print(
        f"sensor cheapest_window_start:  {_t(w.start if w else None)} -> {_t(w.end if w else None)} "
        f"avg {_p(w.average if w else None)} ({args.hours:g} h)"
    )
    print(f"binary_sensor cheap_now:       {s.cheap_now}")
    print(f"binary_sensor tomorrow_available: {s.tomorrow_available}")
    if client.quota_remaining is not None:
        print(f"sensor api_quota_remaining:    {client.quota_remaining}/{client.quota_limit}")
    if comparison is not None:
        _print_comparison(comparison, peers_today)
    return 0


def _print_comparison(c: Comparison, peers_today: dict[str, DayPrices]) -> None:
    left_out = sorted(set(peers_today) - set(c.curves))
    print(f"\ncomparison: {len(c.curves)} tariffs with curves; left out (withdrawn): {left_out or '-'}")
    for tariff, curve in c.curves.items():
        mark = " (estimate)" if curve.status == "estimate" else ""
        print(
            f"sensor average_today_{tariff}: {_p(curve.today_avg)}{mark} "
            f"[{len(curve.today.slots) if curve.today else 0} today / "
            f"{len(curve.tomorrow.slots) if curve.tomorrow else 0} tomorrow slots]"
        )
    s = c.summary()
    print(f"compare: {s['monthly_kwh']:g} kWh/month, {s['kva']} kVA, month {s['month']}, VAT {s['vat_included']}")
    for row in s["tariffs"]:
        mark = " (estimate)" if row["estimate"] else ""
        sel = " <- selected" if row["selected"] else ""
        saving = f"{row['saving']:+.2f}" if row["saving"] is not None else "-"
        print(f"sensor monthly_cost_{row['tariff']}: {row['cost']:.2f} EUR{mark}, saving vs selected {saving}{sel}")
    cheapest = c.cheapest
    print(
        f"sensor cheapest_supplier: {cheapest.name if cheapest else '-'} "
        f"(saving {s['saving']} EUR/month; cheapest exact: {s['cheapest_exact']})"
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
