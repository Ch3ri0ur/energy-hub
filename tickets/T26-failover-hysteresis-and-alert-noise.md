# T26: Grid failover flaps and alerts repeat on every WiFi drop of the Tasmota readers

**Priority:** P2 (operational noise; Tasmotas sit in a metal box with poor WiFi) | **Effort:** S | **Area:** engine, health

## Problem
The two Tasmota IR readers lose WiFi regularly. Each drop longer than
`sources.tasmota_grid.stale_timeout` (120 s) causes:
1. **Failover flapping.** `active_grid()` switches to Anker the instant the Tasmota
   reading is older than 120 s and switches back on the very next Tasmota message.
   With a reader that reconnects every few minutes, `grid.source` (a Telegraf tag)
   and the "Grid source switched" log line toggle constantly, and the grid power
   series alternates between two meters that disagree by a few tens of watts.
2. **Alert spam.** `AlertManager.check` deletes the backoff state of an issue as soon
   as it recovers (`health.py:46-47`). If `grid_stale` recovers and reappears five
   minutes later it is sent again immediately at backoff stage 0, so every WiFi drop
   produces a webhook notification, regardless of the configured backoff.
3. `grid_stale` is raised even while the Anker failover is providing good data, so the
   alert says "stale" although the published grid values are fine.

## Where
- `energy_hub/engine.py:105-129` — `active_grid`
- `energy_hub/health.py:46-47` — backoff state dropped on recovery
- `energy_hub/health.py:142-145` — `pv_stale` / `grid_stale` issues

## Fix
1. Hysteresis in `active_grid`: add `sources.tasmota_grid.failback_after: 60`
   (seconds, new field on `SourceConfig`, default 0 = old behaviour). After switching
   to Anker, only switch back once Tasmota readings have been arriving continuously for
   `failback_after` seconds (track `_tasmota_fresh_since`, reset whenever a gap >
   `stale_timeout` is seen). Anker→Tasmota switch happens immediately if Anker itself
   becomes stale.
2. Alert cool-down: keep an issue's backoff state for `backoff_stages[0]` seconds after
   recovery instead of deleting it (`_state[issue] = (last_sent, stage)` stays; only
   purge entries whose `last_sent` is older than the largest stage). A re-occurrence
   within the window continues the backoff instead of restarting at stage 0.
3. Split the issue: `grid_stale` only when **no** grid source is fresh (i.e. the
   snapshot's `grid.stale` is true); add `grid_tasmota_stale` at severity `info`
   (no webhook) when Tasmota is stale but Anker is covering. Same idea is not needed
   for PV (no failover source).

## Acceptance criteria
- Tasmota gap of 130 s followed by continuous readings: `grid.source` stays `anker` for `failback_after` seconds, then returns to `tasmota` once.
- `grid_stale` recovering at t and re-occurring at t+300 s does not publish a second alert (backoff stage 0 is 3600 s).
- With Tasmota stale and Anker fresh, `health.issues` contains `grid_tasmota_stale` but not `grid_stale`.

## Tests
`tests/test_engine.py` (time-travel by constructing readings with explicit `received_at`)
and `tests/test_health.py` with a recording `publish_cb`.
