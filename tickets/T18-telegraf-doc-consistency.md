# T18: `docs/telegraf-energy.conf` contradicts its own header; `persistent_session` with QoS 0 is a no-op

**Priority:** P3 (docs) | **Effort:** XS | **Area:** docs / Telegraf

## Problem
1. The header (lines 3-7) says the file is a drop-in that "should only contain the
   energy-hub MQTT inputs" and that `[agent]` / `[[outputs.influxdb_v2]]` live in the
   main config — but lines 9-27 define both. Dropping this file into
   `telegraf.d/` alongside a main config gives duplicate `[agent]` sections / duplicate
   outputs (double writes).
2. All three `mqtt_consumer` inputs set `persistent_session = true` with `qos = 0`.
   Telegraf documents that persistent sessions require `qos > 0` to have any effect.
3. Useful fields from the snapshot are not ingested: `grid.stale`, `pv_roof.stale`,
   `battery.stale`, `charger.stale`, `pv_balcony.pv_1_power` … `pv_4_power`. The
   `health` input ingests no per-source status.

## Where
- `docs/telegraf-energy.conf:1-27`, `:38-40`, `:159-161`, `:191-193`, `:164-179`

## Fix
1. Pick one shape. Recommended: keep it a **drop-in**: delete lines 9-27 and add a
   comment that `INFLUX_TOKEN`/`EH_MQTT_PASSWORD` must be in Telegraf's environment.
2. Either set `qos = 1` (and keep `persistent_session`) or drop `persistent_session`.
   Recommended: `qos = 1`, since `client_id` is already fixed per input.
3. Add boolean fields for the four `stale` flags and float fields for `pv_balcony.pv_{1..4}_power`
   (rename `pv_balcony_pv1` …). For health, add tags/fields:
   `sources.pv_roof.status`, `sources.grid_tasmota.status`, `sources.solarbank.status` as tags and
   `sources.*.age` as floats. Note T11 adds `status = offline`.
4. Update `dashboard.json` only if the new fields are wanted (out of scope here).

## Acceptance criteria
- `telegraf --config docs/telegraf-energy.conf --test` (with env vars set) parses without duplicate-section errors.
- Header text matches file content.
