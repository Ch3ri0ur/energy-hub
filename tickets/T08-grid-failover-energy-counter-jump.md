# T08: Grid failover switches the cumulative energy counters to a different meter (step change for evcc/Grafana)

**Priority:** P1 (bug, data contract) | **Effort:** S | **Area:** engine

## Problem
When Tasmota grid data goes stale, `active_grid()` switches to the Anker smart meter
and the snapshot publishes `grid.energy_import/energy_export` from the Anker reading.
The Tasmota SML counters are the official meter totals (~42 000 / ~28 000 kWh); the
Anker counters are its own lifetime totals with a completely different offset. On
failover and again on fail-back, `energy/grid.energy_import` jumps by tens of
thousands of kWh. evcc uses this field as the grid meter energy, and Grafana panels
using `difference()`/`increase()` on `grid_energy_in` get giant spikes.

## Where
- `energy_hub/engine.py:137-161` — `active_grid()`
- `energy_hub/engine.py:339-340` — `energy_import` / `energy_export` taken from `grid_reading`
- `docs/evcc-meters.yaml:19-23` — evcc reads `.energy_import`

## Fix
Decouple *power* source from *energy* source:
1. In `snapshot()`, always take `energy_import`/`energy_export` from `self.grid_tasmota`
   if it has ever been received (stale is fine — a cumulative counter that pauses and
   later jumps forward is the correct behaviour for a meter that was offline).
2. Only when `self.grid_tasmota is None` (never received since start) fall back to the
   Anker energies, and in that case add `"energy_source": "anker"`; otherwise
   `"energy_source": "tasmota"`.
3. Add `"energy_stale": <bool>` (age of the Tasmota reading > stale timeout).
4. Update the comment block in `docs/evcc-meters.yaml` (lines 7-9) to list the new fields.

## Acceptance criteria
- With a fresh Tasmota reading (import 42001) then a Tasmota outage and fresh Anker data (import 512): `snapshot()["grid"]["source"] == "anker"` **and** `["grid"]["energy_import"] == 42001` and `["grid"]["energy_source"] == "tasmota"`.
- With no Tasmota reading at all and Anker data: `energy_import == 512`, `energy_source == "anker"`.

## Tests
`tests/test_engine.py`: construct readings with `received_at = time.time() - 600` to
simulate staleness.
