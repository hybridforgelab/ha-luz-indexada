"""Constants for the Luz Indexada integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final
from zoneinfo import ZoneInfo

DOMAIN: Final = "luz_indexada"
LOGGER = logging.getLogger(__package__)

# Prices are published per Lisbon day; every schedule decision uses Lisbon time.
LISBON: Final = ZoneInfo("Europe/Lisbon")

# RapidAPI listing (end users). The slug can change before the listing is published,
# so it is a constant here and an advanced option in the config flow.
DEFAULT_RAPIDAPI_HOST: Final = "luz-indexada-portugal-electricity-prices-api.p.rapidapi.com"
# Direct mode (self-hosted / testing): base URL + bearer key, no default URL.

WEBSITE: Final = "https://luz.hybridforgelab.com"
MANUFACTURER: Final = "Hybrid Forge Lab"
ATTRIBUTION: Final = "Prices: OMIE (www.omie.es). Network tariffs: ERSE. Losses: E-REDES."

CONF_MODE: Final = "mode"
CONF_BASE_URL: Final = "base_url"
CONF_RAPIDAPI_HOST: Final = "rapidapi_host"
CONF_TARIFF: Final = "tariff"
CONF_TARIFF_NAME: Final = "tariff_name"
CONF_CYCLE: Final = "cycle"
CONF_INCLUDE_VAT: Final = "include_vat"
CONF_WINDOW_HOURS: Final = "window_hours"

MODE_RAPIDAPI: Final = "rapidapi"
MODE_DIRECT: Final = "direct"

CYCLES: Final = ("simples", "bi-diario", "bi-semanal", "tri-diario", "tri-semanal")
DEFAULT_CYCLE: Final = "simples"
DEFAULT_INCLUDE_VAT: Final = True
DEFAULT_WINDOW_HOURS: Final = 2.0
MIN_WINDOW_HOURS: Final = 0.25
MAX_WINDOW_HOURS: Final = 12.0

SLOT: Final = timedelta(minutes=15)
REQUEST_TIMEOUT: Final = 30

# Call budget (RapidAPI Basic = 150 requests/month):
# - today's prices: only when nothing usable is cached (normally never: tomorrow is promoted at midnight)
# - tomorrow's prices: from PUBLICATION_HOUR Lisbon, then every TOMORROW_RETRY until published
# - today's prices again once tomorrow is out, to fill today's last hour (pending until then)
PUBLICATION_HOUR: Final = 13
TOMORROW_RETRY: Final = timedelta(hours=1)
ERROR_BACKOFF_MIN: Final = timedelta(minutes=15)
ERROR_BACKOFF_MAX: Final = timedelta(hours=2)
RATE_LIMIT_BACKOFF: Final = timedelta(hours=1)
RATE_LIMIT_BACKOFF_MAX: Final = timedelta(hours=6)

STORAGE_VERSION: Final = 1
