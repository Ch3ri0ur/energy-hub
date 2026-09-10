# energy-hub tickets

Full-repo review, 2026-09-10. Validator overhaul, timestamp helper dedupe and the
alignment-skew guard were done directly the same day (see "Done" below). Each ticket is self-contained: problem, file:line
references, concrete fix, acceptance criteria, tests to add. Everything except the two rows below was implemented the same day (commits
b6a9a32..57f5e16).

| # | Title | Prio | Effort | Type |
|---|-------|------|--------|------|
| [T01](T01-test-and-lint-scaffolding.md) | Extend the test suite (scaffolding exists) | P1 | M | foundation |
| [T14](T14-anker-optional-import-and-dependency-pin.md) | Pin anker-solix-api to a git rev (import compat already done) | P3 | XS | packaging |

Done directly (2026-09-10), no ticket needed:
- Validator overhaul → `energy_hub/validation.py` (bounds / first-reading floor / monotonic / rate; held-for-confirmation for counter jumps and power jumps > 3 kW, so single garbage values never reach the output and a poisoned baseline recovers in two readings; forced re-baseline after N failures as backstop; device time never rejects, clock offset tracked). Superseded T06, T07.
- `energy_hub/timeutil.py` single ISO parser (was T20); ruff import fixes.
- Alignment hardening (was T09): `max_alignment_skew` guard, fallback to current reading instead of oldest history entry, device time only trusted while in sync with host.
- pytest/ruff scaffolding, 30 tests.
- Anker import compatibility for both library layouts (T14 steps 0–2).
- Gas tracker: documented sync key, no phantom impulse on start/retained messages, atomic state file, retained sync cleared, publish on connect and on battery change (T02–T05).
- Grid failover: energy counters stay on the Tasmota meter (`energy_source`/`energy_stale` fields), stale readings used symmetrically, fail-back hysteresis via `sources.tasmota_grid.failback_after`, alert backoff survives recovery, `grid_tasmota_stale` info issue while Anker covers, per-issue recovery messages (T08, T25, T26).
- MQTT: connect_async with automatic reconnect (broker down at boot no longer crashes the hub), wildcard-capable routing via message_callback_add, Last Will + clean-shutdown offline health payload, retained charger messages ignored (T10, T11, T24).
- Anker plugin: alive only while the MQTT session is connected, status (last_error, retries, last_snapshot_at) in health and in the alert text, partial snapshots when only meter or only solarbank reports, guarded message callback, ms timestamp guard, no shutdown traceback (T12, T13, T15).
- evcc contract: every section carries `stale` and `dead`; per-topic payloads publish `null` for power/soc of dead sources while `energy/aggregated` stays numeric for Telegraf (T16).
- Config loader warns about unknown keys, coerces scalar types with clear errors, defaults an empty backoff list; stale policy reduced to `zero_after`; engine access serialised with an RLock; webhook delivery moved to a background thread (T17, T21, T23).
- Telegraf drop-in is a pure inputs file again with QoS 1 and extra stale/age/quality fields; systemd unit uses `--frozen --no-sync`, `Restart=always`, `TimeoutStopSec`; `logging.console` switch to avoid double logging under journald (T18, T19).
- Adapter tests for Tasmota, charger and Anker snapshot parsing (T01, except the remaining items listed in the ticket).
- README with the full MQTT topic contract (T22).

Dependencies: T01 → all. T14 removes the import stub introduced in T01.  

Owner answers (2026-09-10), already folded into the tickets:
1. Host timezone probably Europe/Berlin, Tasmota `Timezone` unknown → T09 keeps the clock-offset diagnostics as required.
2. Deployed `anker-solix-api` is still 3.5.x (`api` package); local checkout is 3.8.2 (`anker_solix_api`) → T14 imports must support both until the box is upgraded.
3. Dead sources: publish `null` to evcc → T16 option A.
