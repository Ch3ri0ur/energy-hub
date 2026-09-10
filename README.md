# energy-hub

Unifies the home's energy measurement devices into one clean, validated MQTT
stream for [evcc](https://evcc.io), Telegraf/InfluxDB and Grafana.

Inputs (raw):

| Device | Topic (default) | Data |
|---|---|---|
| Tasmota IR reader on the PV meter (SML) | `tele/tasmota_pv/SENSOR` | roof PV power + generation counter |
| Tasmota IR reader on the grid meter (SML) | `tele/tasmota_grid/SENSOR` | grid power + import/export counters |
| Anker Solix (A17X7 smart meter + A17C5 Solarbank 3) via cloud MQTT | `anker/solix/live/system` (published by the built-in bridge) | grid failover meter, balcony PV, battery |
| go-eCharger | `go-eCharger/<id>/nrg` | charger power |
| Zigbee2MQTT contact sensor on the BK4 gas meter | `zigbee2mqtt/GasImpulsCounter` | gas impulses (0.01 m³) |

Outputs: `energy/*` topics, see below.

## MQTT contract

All payloads are JSON. Power in **W**, energy in **kWh**, battery energy in **Wh**,
SoC in **%**, gas in **m³**. Every state topic is published every
`output.publish_interval` seconds (10) and retained.

Sign conventions: grid `power` positive = import, negative = export.
Battery `power` positive = charging, negative = discharging.

### Staleness

Every section carries two flags:

- `stale` – the source's last reading is older than its `stale_timeout`
  (`sources.*.stale_timeout`). The last value is still used.
- `dead` – older than `stale_policy.zero_after`. The value is zeroed in
  `energy/aggregated` (numbers for Telegraf) and published as **`null`** on the
  per-topic payloads so evcc's jq fails loudly instead of reading a fabricated 0.

### Topics

`energy/grid`

| field | meaning |
|---|---|
| `power`, `import_w`, `export_w` | net / import-only / export-only power (null when dead) |
| `energy_import`, `energy_export` | cumulative counters, **always from the Tasmota SML meter** once it has reported, even while power comes from Anker (no step change on failover) |
| `energy_source`, `energy_stale` | meter and freshness of the counters |
| `source` | meter the power comes from: `tasmota`, `anker` or `""` |
| `stale`, `dead` | see above |

`energy/pv/roof`: `power`, `energy` (counter), `stale`, `dead`.

`energy/pv/balcony` (Solarbank): `power` (panel generation), `pv_1_power` … `pv_4_power`,
`output_power` (what reaches the house, ≤ 800 W), `stale`, `dead`.

`energy/pv/total`: `power` = roof + balcony *output* (AC side, for the house
balance), `generation` = roof + balcony *panels*, `stale` (either source),
`dead` (both sources).

`energy/battery`: `soc`, `power`, `charge_w`, `discharge_w`, `energy_wh`,
`state` (`charging` / `discharging` / `idle` / `unknown`), `stale`, `dead`.

`energy/charger`: `power` (sample aligned with the other readings), `raw_power`
(latest), `stale`, `dead`.

`energy/house`: `power` = grid + pv_total − charger, `stale`, `dead` (follows grid).

`energy/aggregated`: all of the above in one object plus `consumption`
(= grid + pv_total), `self_consumption_pct`, `self_sufficiency_pct`, `timestamp`
(unix). This is what Telegraf ingests.

`energy/health` (every `output.health_interval` s, retained): `status`
(`ok` / `degraded` / `offline`), `connected`, `uptime`, `issues`,
`anker_plugin_alive`, `anker_plugin` (`alive`, `last_error`, `retries`,
`last_snapshot_at`), `sources` (per source: `status` = `ok` / `stale` / `dead` /
`no_data`, `age`, `quality`, `validation` counters), `timestamp`. The broker
publishes `status: offline` via Last Will when the hub dies.

`energy/alerts` (not retained): `{issue, severity, message, timestamp, context}`.
Issues: `grid_stale` (no meter delivering), `grid_tasmota_stale` (info: Anker
covering), `pv_stale`, `pv_quality_poor`, `grid_quality_poor`,
`anker_plugin_down`, plus one `info` message per recovered issue. Warnings are
also POSTed to `alerts.webhook_url` with exponential backoff
(`alerts.backoff_stages`); the backoff survives short recoveries.

`energy/gas` (retained, on change and on every reconnect): `volume_m3`,
`impulse_count`, `battery`, `battery_low`, `ts`.

`energy/gas/set`: publish `{"volume_m3": 12345.67}` (without `-r`) to sync the
counter to the meter display. The hub clears a retained sync message after use.

### Grid failover

Power source priority: Tasmota (fresh) → Anker (fresh) → whichever stale
reading is newer. After a Tasmota outage, Tasmota must deliver continuously for
`sources.tasmota_grid.failback_after` seconds before it replaces a working Anker
feed again (the readers sit in a metal box with weak WiFi). The energy counters
never switch meters, see `energy/grid`.

### Validation

Tasmota readings pass `energy_hub/validation.py`: physical bounds, a
first-reading floor, monotonic counters and a rate limit derived from the power
ceilings. A reading that fails the counter checks or jumps by more than
`validation.power_jump_confirm` W is held until a second reading confirms it, so
a single garbage value never reaches the output and a poisoned baseline recovers
within two readings. A grid jump explained by the charger's own power change
passes immediately. Device timestamps never cause rejections; the clock offset to
the host is logged and device time is only used for sample alignment while it is
in sync.

## Configuration

`config.yaml` (see the comments there) plus `EH_*` environment variables for
secrets and connection settings: `EH_MQTT_PASSWORD`, `EH_MQTT_USERNAME`,
`EH_MQTT_BROKER`, `EH_MQTT_PORT`, `EH_ANKER_USER`, `EH_ANKER_PASSWORD`,
`EH_ANKER_COUNTRY`, `EH_ALERT_WEBHOOK`, `EH_LOG_LEVEL` (no other key has an env
override). A `.env` file next to the config is loaded automatically
(`.env.example`; keep it `KEY=value` without inline comments, systemd reads it
too). Unknown keys are logged, wrong types fail at start-up with exit status 78
(systemd does not restart on it). The Anker bridge is enabled when user and
password are set.

## Running

```sh
uv sync --frozen            # anker-solix-api is expected at ../anker-solix-api (see pyproject)
uv run energy-hub -c config.yaml
```

Deployment: `energy-hub.service` (systemd, hardened, `uv run --frozen --no-sync`),
`docs/telegraf-energy.conf` (Telegraf inputs drop-in), `docs/evcc-meters.yaml`
(evcc meter definitions), `dashboard.json` (Grafana).

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check energy_hub tests
```

Open work is tracked in `tickets/`.
