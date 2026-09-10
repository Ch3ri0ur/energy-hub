# T16: Dead sources are published to evcc as `power: 0` / `soc: 0` — evcc cannot tell "no data" from "zero"

**Priority:** P2 (safety of evcc decisions) | **Effort:** M | **Area:** engine, publisher, docs
**Decision (owner, 2026-09-10):** publish `null` for dead sources (option A). Option B is not wanted.

## Problem
When a source exceeds the grace deadline the engine zeroes it (`grid.power = 0`,
`pv_total.power = 0`, `battery.power = 0`, `battery.soc = 0`) and sets a `stale` flag.
evcc reads only `.power` / `.soc` via jq (`docs/evcc-meters.yaml`), and the topics are
retained and republished every 10 s, so evcc's `timeout: 60s` never fires. evcc then
believes: grid is balanced (0 W), PV is 0 W, battery is empty (0 %). In PV/min+PV mode
this can start or stop charging on fabricated numbers. `energy/pv/total` has no
`stale` flag at all.

## Where
- `energy_hub/engine.py:270-278` — grid zeroing
- `energy_hub/engine.py:281-294` — PV / solarbank zeroing
- `energy_hub/engine.py:361-374` — `pv_total` (no stale flag), `battery.soc` 0 when zeroed
- `energy_hub/publisher.py:35-44` — per-topic publishes
- `docs/evcc-meters.yaml`

## Fix (option A — decided)
1. Keep the `energy/aggregated` blob numeric (Telegraf needs numbers).
2. For the evcc-facing per-topic payloads (`grid`, `pv/total`, `battery`) publish
   `null` instead of `0` for `power`/`soc` when the corresponding source is **dead**
   (beyond grace deadline). Implement in `Publisher.publish_snapshot` using the new
   snapshot flags: add `"dead": bool` next to each existing `"stale"` and add
   `"stale"`/`"dead"` to `pv_total` (`pv_stale or sb_stale` / `pv_dead and sb_dead`).
   Helper: `_null_if(payload, key, cond)` copies the dict and sets `key: None`.
3. In `docs/evcc-meters.yaml` add a comment: "When a source is dead the hub publishes
   `null`; evcc's jq then errors and the meter is treated as faulty, which is safer
   than a fake 0." Verify once in evcc logs that `null` produces a meter error.
4. Grafana: nothing changes (aggregated blob unchanged).

## Acceptance criteria
- Grid dead (age > grace deadline): `energy/grid` payload has `"power": null`, `"dead": true`; `energy/aggregated.grid.power == 0`.
- All sources fresh: payloads identical to today plus the new `dead` flags and `pv_total.stale`.

## Tests
`tests/test_publisher.py` and `tests/test_engine.py` for the two cases above.
