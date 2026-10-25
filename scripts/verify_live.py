"""Fetch live prices with the integration's own client and print what the entities would show.

Direct mode (self-hosted / testing):
    LUZ_BASE_URL=https://api.example LUZ_API_KEY=... python scripts/verify_live.py
RapidAPI mode:
    LUZ_RAPIDAPI_KEY=... python scripts/verify_live.py [--host <slug>.p.rapidapi.com]

Makes 1-2 requests (today, plus tomorrow after 13:00 Lisbon). Never prints the key.
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

from custom_components.luz_indexada.api import LuzApiClient, LuzNotPublishedError
from custom_components.luz_indexada.const import (
    DEFAULT_RAPIDAPI_HOST,
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
        today = await client.prices(local.date(), args.tariff, args.cycle, now=now)
        tomorrow = None
        if local.hour >= PUBLICATION_HOUR:
            try:
                tomorrow = await client.prices(local.date() + timedelta(days=1), args.tariff, args.cycle, now=now)
            except LuzNotPublishedError as err:
                print(f"tomorrow: not published yet (next publication {_t(err.next_publication)})")

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
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
