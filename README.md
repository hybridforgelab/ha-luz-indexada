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
- **Supplier comparison** (on by default), see [Supplier comparison](#supplier-comparison):
  - today's average price and the 15-minute curves of every other tariff (from the same requests, no extra calls);
  - an estimated monthly cost per supplier for your consumption and contracted power;
  - a *Cheapest supplier* sensor with the saving versus your tariff.
- A ready-to-paste [example dashboard](examples/dashboard.yaml) ([Portuguese version](examples/dashboard.pt.yaml), for Home Assistant set to Portuguese).
- Very few API calls (see [API usage](#api-usage)). The current price is computed locally every 15 minutes and the
  prices are cached, so a restart costs no calls.
- Handles the DST days (92 or 100 quarter-hours) and the pending last hour of the day.
- Reauthentication when the key is rejected, backoff on rate limits, and diagnostics with the key redacted.
- English and Portuguese (pt-PT).

**Supported tariffs** come from the API's catalogue, so new ones appear without updating the integration. Today
there are ten:

- Coopérnico ÚNICO
- G9 Smart Dynamic
- Repsol Leve Sem Mais
- Galp Plano Flexível
- Endesa Tarifa Dinâmica
- EDP Eletricidade Indexada Horária
- Alfa Power Index
- EZU Indexada
- MEO Energia Tarifa Dinâmica
- Plenitude Tendência

Each comes with the simple, two-rate and three-rate cycles (daily or weekly) its supplier sells up to 20,7 kVA.

Some tariffs are marked **estimate** (in the setup list and in the `status` / `estimate` attributes):

- when the supplier's ficha only gives indicative values;
- Alfa, EZU, MEO and Plenitude, because they include REN's system costs, which REN publishes about two days late:
  their prices for today and tomorrow use a forecast.

## Installation

With HACS installed, these two buttons do steps 2 and 4 for you:

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hybridforgelab&repository=ha-luz-indexada&category=integration)
[![Open your Home Assistant instance and start setting up Luz Indexada.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=luz_indexada)

1. Install [HACS](https://hacs.xyz/).
2. HACS → ⋮ → **Custom repositories** → add `https://github.com/hybridforgelab/ha-luz-indexada`, category
   **Integration**.
3. Install **Luz Indexada** and restart Home Assistant.
4. **Settings → Devices & services → Add integration → Luz Indexada**.

Manual install: copy `custom_components/luz_indexada` into your `config/custom_components/` folder and restart.

## Configuration

1. Choose how you reach the API:
   - **RapidAPI key** (recommended): subscribe to *Luz Indexada* on RapidAPI (a normal install needs the Pro plan; Basic is only for trying it) and paste
     your `X-RapidAPI-Key`. With *advanced mode* turned on in your user profile you can also change the RapidAPI
     host.
   - **Direct API**: a base URL and a bearer key, for self-hosted or test deployments.
2. Pick the **tariff** and the **cycle**, choose whether prices **include 23% VAT**, and set the **cheapest window**
   length.
3. Supplier comparison:
   - **Compare suppliers** (default on);
   - **Monthly consumption (kWh)** (default 250);
   - **Contracted power (kVA)** (default 6.9, a standard step).

   Consumption and power are only used for the monthly cost comparison.

Each tariff and cycle is its own entry:

- Add several entries to compare them. Each one has its own API budget.
- VAT and the window length can be changed later under **Configure**, without spending an API call.
- Changing the consumption or the power costs one call, to refresh the monthly comparison.

**About VAT:**

- The API prices exclude VAT. With *Include 23% VAT* on, every component is multiplied by 1.23.
- On a real bill, the first 200 kWh per 30 days (300 for large families) at ≤ 6.9 kVA pay 6%.
- Prices are computed for a reference power of 6.9 kVA (10.35 kVA for Endesa three-rate). This only affects
  Endesa's term A slightly; the per-kWh price has no power option.

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
| Average price today — *supplier* | sensor, `EUR/kWh` | One per comparable tariff; `raw_today` / `raw_tomorrow` curves, `status`, `estimate` |
| Estimated monthly cost — *supplier* | sensor, `€` | One per comparable tariff; `saving_vs_selected`, VAT split, `monthly_kwh`, `kva` |
| Cheapest supplier | sensor | Name of the cheapest tariff; `tariffs` list with cost, saving and status |

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

## Supplier comparison

With **Compare suppliers** on (the default, also for entries created by older versions), the integration asks
`/v1/prices` for every tariff instead of only yours.

That is the same number of requests: one response has all the tariffs (about 20 KB compressed).

Every tariff that is still on sale gets:

- **Average price today — *supplier*** (e.g. `sensor.luz_indexada_galp_plano_flexivel_simples_average_price_today_repsol_leve_sem_mais`):
  today's mean price, VAT per your option, with `raw_today` / `raw_tomorrow` in the same `[{start, end, value}]`
  format as *Current price*, so you can overlay every supplier on one ApexCharts chart.
- **Estimated monthly cost — *supplier*** (e.g. `…_estimated_monthly_cost_repsol_leve_sem_mais`):
  - the API's `/v1/compare` cost of a standard ERSE consumption profile for the current month, at your monthly kWh
    and contracted power;
  - with the real VAT split (6% on the first 200 kWh at ≤ 6.9 kVA, 23% on the rest) when VAT is on;
  - days not priced yet are projected;
  - attributes: `saving_vs_selected` (positive means switching saves money), `total_excl_vat`, `vat`,
    `avg_energy_price`, `days_priced`, `coverage`, `flags`.
- **Cheapest supplier** (`…_cheapest_supplier`): the cheapest tariff's name, with `tariffs` (every tariff, cheapest
  first, with `cost`, `saving`, `status`, `estimate`, `selected`), `selected_cost`, `saving`, `cheapest_exact`,
  `monthly_kwh`, `kva` and `month`.

Good to know:

- Tariffs that were withdrawn and replaced (for example Coopérnico Base 2.0, replaced by ÚNICO), or that aren't
  available for your cycle, are left out.
- **Estimate** tariffs are marked with `estimate: true` (and *estimate* in the example dashboard).
  Their supplier ficha doesn't give every value, so treat them as a guide.
- Everything here is informative, not an offer or a bill.
- New tariffs added to the API show up as new sensors on their own.
- Each entry compares on its own. If you add several entries, you may want to turn the comparison off on all but one.
- The monthly cost uses a fixed consumption figure. Reading it from your own energy sensor isn't supported: a
  monthly meter resets at the start of the month, so its value isn't a monthly consumption until the month ends.

## Example dashboard

[`examples/dashboard.yaml`](examples/dashboard.yaml) is a ready-to-paste Lovelace view (sections layout) with:

- the price tiles;
- today's and tomorrow's prices for your tariff;
- all suppliers' 15-minute curves on one chart;
- the estimated monthly cost per supplier (chart and table, cheapest highlighted);
- a short note on estimate versus exact.

- It needs [apexcharts-card](https://github.com/RomRider/apexcharts-card) (HACS → Frontend).
  HACS registers the Lovelace resource; on a manual install add it under Settings → Dashboards → ⋮ → Resources.
- Paste it in the dashboard's raw configuration editor as a view.
- If your Home Assistant is set to Portuguese, entity ids are created from the Portuguese names (`…_preco_atual`,
  `…_custo_mensal_estimado_…`): use [`examples/dashboard.pt.yaml`](examples/dashboard.pt.yaml) instead.
- The entity ids are those of a default install of Galp Plano Flexível, simple cycle
  (`sensor.luz_indexada_galp_plano_flexivel_simples_…`):
  - for another tariff or cycle, replace `galp_plano_flexivel_simples` with your prefix (Settings → Devices &
    services → Luz Indexada → Entities);
  - the per-supplier suffixes (`…_repsol_leve_sem_mais`, `…_g9_smart_dynamic`, …) are the same on every install.

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

This integration is built to use very few calls. The RapidAPI Basic plan allows only **15 requests per month**, enough to
try the integration for a few days; a normal install needs the **Pro** plan (10,000 requests per month).

| When | Calls |
|---|---|
| Setup (validate the key and load the tariff list) | 1, once |
| First start with nothing cached | 1 (today) |
| From 13:00 Lisbon until tomorrow's prices are out (retried hourly, or at the time the API announces) | usually 1 |
| Right after, to fill today's last hour (published together with tomorrow) | 1 |
| Midnight | 0 (cached tomorrow becomes today) |
| Restarts and option changes | 0 (prices are cached on disk) |
| The current price, every 15 minutes | 0 (computed locally) |
| Supplier comparison: other tariffs' prices | 0 (same responses as yours) |
| Supplier comparison: monthly cost (`/v1/compare`) | 1 a day, plus 1 when you change the consumption or the power |
| Supplier comparison: tariff list, to leave withdrawn tariffs out | 1 a week |
| Turning the comparison on with prices already cached | 1–2, once (fills the other tariffs for the cached days) |

In practice:

- **About 2 calls a day (~60–70 a month)** without the comparison, and **about 3 a day (~95–105 a month)** with it
  (the default).
- Late publications, retries and errors can push it to ~4 a day (**~120–130 a month**, ~160 with the comparison):
  well inside Pro, over Basic.
- A failed comparison is retried after 6 hours and never delays the prices.
- Errors back off from 15 minutes up to 2 hours; a 429 waits at least an hour, doubling up to 6.
- Every extra tariff entry has its own calls.
- Keep an eye on the *API calls this month* and *RapidAPI requests remaining* sensors.

## Data and attribution

- Prices: OMIE (www.omie.es).
- Network access tariffs and period hours: ERSE.
- Loss profiles: E-REDES.

See [luz.hybridforgelab.com](https://luz.hybridforgelab.com) for the formulas, sources and known gaps per tariff.

## Development

```bash
uv venv -p 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/pytest --cov
# Live check with the integration's own client (prints what the entities would show; never prints the key):
LUZ_RAPIDAPI_KEY=... .venv/bin/python scripts/verify_live.py --tariff galp-plano-flexivel --cycle simples
LUZ_BASE_URL=https://... LUZ_API_KEY=... .venv/bin/python scripts/verify_live.py
# The supplier comparison is printed too (2 more calls); --kwh 300 --kva 10.35 to change it, --no-compare to skip it.
```

Releases use Conventional Commits and release-please.

The icon ships with the integration in `custom_components/luz_indexada/brand/` (Home Assistant 2026.3+ and HACS read it from there).

## Contributing

Issues and pull requests are welcome:

- the [contributing guide](https://github.com/hybridforgelab/.github/blob/main/CONTRIBUTING.md) (Conventional Commits, tests and lint before the pull request);
- the [code of conduct](https://github.com/hybridforgelab/.github/blob/main/CODE_OF_CONDUCT.md).

Problems with the Luz Indexada API itself (plans, keys, prices) go to the [contact page](https://luz.hybridforgelab.com/contact.html).

Security issues go by email, never in a public issue.

Contact: hybridforgelab@gmail.com · Licence: MIT

---

## Português

**Luz Indexada** mostra no Home Assistant o **preço final real da eletricidade a cada 15 minutos** para os
tarifários indexados em Portugal (Coopérnico, G9, Repsol, Galp, Endesa, EDP, Alfa, EZU, MEO Energia, Plenitude),
em todos os ciclos: simples, bi-horário e tri-horário, diário ou semanal.

- **Instalação:**
  - HACS → Repositórios personalizados → `https://github.com/hybridforgelab/ha-luz-indexada` (Integração).
  - Depois: Definições → Dispositivos e serviços → Adicionar integração → *Luz Indexada*.
  - Com o HACS instalado, estes botões fazem os dois passos:

    [![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=hybridforgelab&repository=ha-luz-indexada&category=integration)
    [![Open your Home Assistant instance and start setting up Luz Indexada.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=luz_indexada)
- **Chave:** subscreva a API *Luz Indexada* no RapidAPI e cole a `X-RapidAPI-Key`. Uma instalação normal precisa do
  plano Pro; o Basic, com 15 pedidos/mês, serve só para experimentar.
- **Entidades:**
  - preço atual e próximo (€/kWh, com ou sem IVA a 23%);
  - mínimo, máximo e média de hoje;
  - início da janela mais barata de N horas;
  - *Barato agora* (abaixo da média de hoje ou dentro da janela mais barata) e *Amanhã disponível*;
  - os atributos `raw_today` / `raw_tomorrow` servem para gráficos ApexCharts.
- **Comparar fornecedores** (ativo por omissão):
  - preço médio de hoje e curvas de 15 minutos de todos os outros tarifários (das mesmas respostas, sem pedidos
    extra);
  - custo mensal estimado por fornecedor para o seu consumo e potência contratada (opções *Consumo mensal* e
    *Potência contratada*);
  - o sensor *Fornecedor mais barato*, com a poupança face ao seu tarifário;
  - os tarifários retirados (por exemplo Coopérnico Base 2.0) e os indisponíveis no seu ciclo ficam de fora;
  - os valores são informativos, não são uma proposta nem uma fatura.
- **Dashboard de exemplo:**
  - [`examples/dashboard.pt.yaml`](examples/dashboard.pt.yaml) se o Home Assistant estiver em português (os
    entity_id saem dos nomes traduzidos); [`examples/dashboard.yaml`](examples/dashboard.yaml) se estiver em inglês;
  - é uma vista Lovelace pronta a colar e precisa do [apexcharts-card](https://github.com/RomRider/apexcharts-card)
    (HACS → Frontend), com o recurso Lovelace registado;
  - os ids correspondem a uma instalação de Galp Plano Flexível, ciclo simples. Para outro tarifário, substitua
    `galp_plano_flexivel_simples` pelo seu prefixo (Definições → Dispositivos e serviços → Luz Indexada →
    Entidades).
- **Consumo da API:**
  - cerca de 2 pedidos por dia (60 a 70 por mês) sem a comparação, e cerca de 3 por dia (95 a 105 por mês) com ela;
  - o custo mensal é pedido uma vez por dia e a lista de tarifários uma vez por semana;
  - o preço atual é calculado localmente e os preços ficam em cache, por isso reiniciar o Home Assistant não gasta
    pedidos.
- **IVA:** com a opção ativa aplica-se 23% a tudo. Numa fatura real, os primeiros 200 kWh por 30 dias (até 6,9 kVA)
  pagam 6%.
- **Estimativas:** os tarifários marcados como **estimate** usam valores indicativos (atributo `estimate`). Na Alfa,
  EZU, MEO e Plenitude entram os custos de sistema da REN, que a REN publica com dois dias de atraso, por isso os
  preços de hoje e de amanhã usam uma previsão.
- **Contribuir:** issues e pull requests são bem-vindos (em português ou inglês); ver o [guia de contribuição](https://github.com/hybridforgelab/.github/blob/main/CONTRIBUTING.md). Questões sobre a API Luz Indexada vão para a [página de contacto](https://luz.hybridforgelab.com/pt/contact.html).

Preços: OMIE (www.omie.es). Contacto: hybridforgelab@gmail.com
