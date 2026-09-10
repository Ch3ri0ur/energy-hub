# T22: Add a README documenting the MQTT contract, sign conventions and operations

**Priority:** P2 (docs; the topics are an interface used by evcc, Telegraf, Grafana) | **Effort:** M | **Area:** docs

## Problem
There is no README. The payload shape of `energy/*` topics, units, sign conventions,
stale/dead semantics, failover behaviour and the gas sync procedure are only
discoverable by reading `engine.py:333-390`, `gas_tracker.py:1-25`, `publisher.py`
and `config.yaml`. Anyone (including a small model working these tickets) needs a
single source of truth.

## Fix
Create `README.md` with these sections (keep it factual, derive from code):
1. **What it does** — one paragraph: subscribes to Tasmota SML (PV + grid), Anker
   Solix (cloud → local bridge), go-eCharger, Zigbee2MQTT gas impulse; publishes
   validated, aggregated `energy/*` topics for evcc, Telegraf/InfluxDB, Grafana.
2. **Topic table** — for each of `energy/grid`, `energy/pv/roof`, `energy/pv/balcony`,
   `energy/pv/total`, `energy/battery`, `energy/charger`, `energy/house`,
   `energy/aggregated`, `energy/health`, `energy/alerts`, `energy/gas`,
   `energy/gas/set`, `anker/solix/live/system`: fields, unit (W / kWh / % / m³),
   retained?, publish interval. Sign conventions: grid `power` +import/−export;
   battery `power` +charging/−discharging; `pv_total.power` = roof + solarbank
   *output* (AC side), `pv_total.generation` = roof + solarbank *panel* power.
3. **Staleness** — `stale` after `stale_timeout`, zeroed after `zero_after`
   (update after T17/T16 land).
4. **Grid failover** — Tasmota → Anker → stale Tasmota (and energy-counter rule from T08).
5. **Gas meter** — how to sync: `mosquitto_pub -t energy/gas/set -m '{"volume_m3": 12345.67}'`
   (no `-r`), state file location.
6. **Configuration** — `config.yaml` sections, `EH_*` env overrides, `.env`.
7. **Running** — `uv sync`, `uv run energy-hub -c config.yaml`, systemd unit, Telegraf drop-in, evcc snippet.
8. **Development** — `uv run pytest`, `uvx ruff check`.

## Acceptance criteria
- Every topic published anywhere in `energy_hub/` appears in the table (`grep -rn "_pub(\|\.publish(" energy_hub` to enumerate).
- Field names match `engine.snapshot()` exactly.
