# Luz Indexada for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/hybridforgelab/ha-luz-indexada/actions/workflows/validate.yml/badge.svg)](https://github.com/hybridforgelab/ha-luz-indexada/actions/workflows/validate.yml)
[![Tests](https://github.com/hybridforgelab/ha-luz-indexada/actions/workflows/tests.yml/badge.svg)](https://github.com/hybridforgelab/ha-luz-indexada/actions/workflows/tests.yml)

The **real final electricity price, every 15 minutes**, for Portuguese indexed (dynamic) tariffs, in Home Assistant.
Each price is OMIE + losses + supplier terms + network tariff (TAR) + IEC, per tariff and per cycle, from the
[Luz Indexada API](https://luz.hybridforgelab.com).

*Versão em português [mais abaixo](#português).*

## Features

- Current and next quarter-hour price in `EUR/kWh`, with or without 23% VAT. You can use it as the Energy
  dashboard's "entity with the current price".
- Today's and tomorrow's 15-minute slots as attributes, ready for ApexCharts.
- Today's minimum, maximum and average.
- Start of the cheapest window of N hours (in 0.25 h steps, default 2 h), searched from now until the last known
  price.
- `Cheap now` binary sensor: on when the current price is below today's average or falls inside the cheapest window.
- `Tomorrow available` binary sensor (OMIE publishes around 13:00 Lisbon).
- Very few API calls (see [API usage](#api-usage)). The current price is computed locally every 15 minutes and the
  prices are cached, so a restart costs no calls.
- Handles the DST days (92 or 100 quarter-hours) and the pending last hour of the day.
- Reauthentication when the key is rejected, backoff on rate limits, and diagnostics with the key redacted.
- English and Portuguese (pt-PT).

Supported tariffs come from the API's catalogue: Coopérnico Base 2.0, G9 Smart Dynamic, Repsol Leve Sem Mais, Galp
Plano Flexível and Endesa Tarifa Dinâmica, each with the simple, two-rate and three-rate cycles (daily or weekly).
Some tariffs are marked **estimate** when the supplier's ficha only gives indicative values. The status shows up in
the setup list and as the `status` / `estimate` attributes.

## Installation

1. Install [HACS](https://hacs.xyz/).
2. HACS → ⋮ → **Custom repositories** → add `https://github.com/hybridforgelab/ha-luz-indexada`, category
   **Integration**.
3. Install **Luz Indexada** and restart Home Assistant.
4. **Settings → Devices & services → Add integration → Luz Indexada**.

Manual install: copy `custom_components/luz_indexada` into your `config/custom_components/` folder and restart.

## Configuration

1. Choose how you reach the API:
   - **RapidAPI key** (recommended): subscribe to *Luz Indexada* on RapidAPI (the Basic plan is enough) and paste
     your `X-RapidAPI-Key`. With *advanced mode* turned on in your user profile you can also change the RapidAPI
     host.
   - **Direct API**: a base URL and a bearer key, for self-hosted or test deployments.
2. Pick the **tariff** and the **cycle**, choose whether prices **include 23% VAT**, and set the **cheapest window**
   length.

Each tariff and cycle is its own entry, so you can add several to compare them. Each entry has its own API budget.
VAT and the window length can be changed later under **Configure**, without spending an API call.

**About VAT:** the API prices exclude VAT. With *Include 23% VAT* on, every component is multiplied by 1.23. On a
real bill the first 200 kWh per 30 days (300 for large families) at ≤ 6.9 kVA pay 6%. Prices are computed for a
reference power of 6.9 kVA (10.35 kVA for Endesa three-rate), which only affects Endesa's term A slightly. The
per-kWh price has no power option.

## Entities

| Entity | Type | Description |
|---|---|---|
| Current price | sensor, `EUR/kWh` | Price of the current quarter-hour. Attributes below |
| Next price | sensor, `EUR/kWh` | Next quarter-hour (`start` attribute) |
| Today minimum / maximum price | sensor, `EUR/kWh` | With the `at` time |
| Today average price | sensor, `EUR/kWh` | Mean of today's priced slots |
| Cheapest window start | sensor, timestamp | Attributes `end`, `average_price`, `hours` |
| Cheap now | binary sensor | Below today's average **or** inside the cheapest window |
| Tomorrow available | binary sensor | Tomorrow's prices are cached (`complete`, `slots_priced`) |
| API calls this month | diagnostic sensor | Counted locally; `next_tomorrow_attempt`, `backoff_until`, `last_error` |
| RapidAPI requests remaining | diagnostic sensor | From RapidAPI's quota headers (RapidAPI mode only) |

Attributes of **Current price**:

| Attribute | Meaning |
|---|---|
| `raw_today`, `raw_tomorrow` | `[{"start", "end", "value"}]` per quarter-hour, local ISO times (Nord Pool / ENTSO-e style) |
| `today`, `tomorrow` | Plain lists of values |
| `tomorrow_valid` | Tomorrow is known |
| `tariff`, `status`, `estimate`, `formula`, `reference_kva` | Tariff details from the API |
| `valid_until`, `terms_expired` | Validity of the supplier terms used for the price, when the API provides it |
| `period` | Current tariff period (`simples`, `vazio`, `fora_vazio`, `ponta`, `cheias`, …) |
| `vat_included`, `today_complete`, `flags` | `today_complete` is false while the last hour is still pending |

The per-slot lists aren't written to the recorder database, so your history stays small.

### ApexCharts card

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Luz Indexada
graph_span: 2d
span:
  start: day
now:
  show: true
series:
  - entity: sensor.luz_indexada_galp_plano_flexivel_simples_current_price
    name: €/kWh
    type: column
    float_precision: 4
    data_generator: |
      return [...entity.attributes.raw_today, ...entity.attributes.raw_tomorrow]
        .filter((s) => s.value !== null)
        .map((s) => [new Date(s.start).getTime(), s.value]);
```

### Automation example

```yaml
automation:
  - alias: Dishwasher in the cheapest window
    triggers:
      - trigger: time
        at: sensor.luz_indexada_galp_plano_flexivel_simples_cheapest_window_start
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.dishwasher
```

## API usage

The RapidAPI Basic plan allows **150 requests per month**, and this integration is built to stay well inside it:

| When | Calls |
|---|---|
| Setup (validate the key and load the tariff list) | 1, once |
| First start with nothing cached | 1 (today) |
| From 13:00 Lisbon until tomorrow's prices are out (retried hourly, or at the time the API announces) | usually 1 |
| Right after, to fill today's last hour (published together with tomorrow) | 1 |
| Midnight | 0 (cached tomorrow becomes today) |
| Restarts and option changes | 0 (prices are cached on disk) |
| The current price, every 15 minutes | 0 (computed locally) |

That's about **2 calls a day, ~60–70 a month**. Late publications, retries and errors can push it to ~4 a day
(**~120–130 a month**), still under 150. Errors back off from 15 minutes up to 2 hours, and a 429 waits at least an
hour, doubling up to 6. Every extra tariff entry has its own calls. Keep an eye on the *API calls this month* and
*RapidAPI requests remaining* sensors.

## Data and attribution

Prices: OMIE (www.omie.es). Network access tariffs and period hours: ERSE. Loss profiles: E-REDES. See
[luz.hybridforgelab.com](https://luz.hybridforgelab.com) for the formulas, sources and known gaps per tariff.

## Development

```bash
uv venv -p 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest --cov
# Live check with the integration's own client (prints what the entities would show; never prints the key):
LUZ_RAPIDAPI_KEY=... .venv/bin/python scripts/verify_live.py --tariff galp-plano-flexivel --cycle simples
LUZ_BASE_URL=https://... LUZ_API_KEY=... .venv/bin/python scripts/verify_live.py
```

Releases use Conventional Commits and release-please. The icon will be submitted to `home-assistant/brands`.

Contact: hybridforgelab@gmail.com · Licence: MIT

---

## Português

**Luz Indexada** mostra no Home Assistant o **preço final real da eletricidade a cada 15 minutos** para os
tarifários indexados em Portugal (Coopérnico, G9, Repsol, Galp, Endesa), em todos os ciclos: simples, bi-horário e
tri-horário, diário ou semanal.

- **Instalação:** HACS → Repositórios personalizados → `https://github.com/hybridforgelab/ha-luz-indexada`
  (Integração). Depois vá a Definições → Dispositivos e serviços → Adicionar integração → *Luz Indexada*.
- **Chave:** subscreva a API *Luz Indexada* no RapidAPI (o plano Basic, com 150 pedidos/mês, chega) e cole a
  `X-RapidAPI-Key`.
- **Entidades:** preço atual e próximo (€/kWh, com ou sem IVA a 23%), mínimo/máximo/média de hoje, início da janela
  mais barata de N horas, *Barato agora* (abaixo da média de hoje ou dentro da janela mais barata) e *Amanhã
  disponível*. Os atributos `raw_today`/`raw_tomorrow` servem para gráficos ApexCharts.
- **Consumo da API:** cerca de 2 pedidos por dia (60 a 70 por mês). O preço atual é calculado localmente e os
  preços ficam em cache, por isso reiniciar o Home Assistant não gasta pedidos.
- **IVA:** com a opção ativa aplica-se 23% a tudo. Numa fatura real, os primeiros 200 kWh por 30 dias (até
  6,9 kVA) pagam 6%.
- Os tarifários marcados como **estimate** usam valores indicativos da ficha do fornecedor (atributo `estimate`).

Preços: OMIE (www.omie.es). Contacto: hybridforgelab@gmail.com
