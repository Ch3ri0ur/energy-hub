# energy-hub tickets

Full-repo review, 2026-09-10. Validator overhaul, timestamp helper dedupe and the
alignment-skew guard were done directly the same day (see "Done" below). Each ticket is self-contained: problem, file:line
references, concrete fix, acceptance criteria, tests to add. Work them top to bottom;
T01 first because every other ticket's "Tests" section depends on it.

| # | Title | Prio | Effort | Type |
|---|-------|------|--------|------|
| [T01](T01-test-and-lint-scaffolding.md) | Extend the test suite (scaffolding exists) | P1 | M | foundation |
| [T10](T10-mqtt-connect-robustness-and-routing.md) | Crash if broker down at start; wildcard routing | P2 | S | robustness |
| [T11](T11-last-will-offline-health.md) | Last Will so `energy/health` goes offline when hub dies | P2 | XS | robustness |
| [T12](T12-anker-alive-semantics.md) | `anker_plugin_alive` true before connected; expose errors | P2 | S | bug |
| [T13](T13-anker-partial-snapshot.md) | Anker bridge withholds meter data if solarbank absent | P2 | XS | bug |
| [T14](T14-anker-optional-import-and-dependency-pin.md) | Pin anker-solix-api to a git rev (import compat already done) | P3 | XS | packaging |
| [T15](T15-anker-plugin-guards.md) | Anker plugin: shutdown traceback, ms timestamps, callback guard | P3 | S | robustness |
| [T16](T16-evcc-dead-source-contract.md) | Dead sources published as `0` to evcc → publish `null` (decided) | P2 | M | design |
| [T17](T17-config-validation-and-cleanup.md) | Config: unknown keys, unused key, stale-policy semantics | P3 | S | quality |
| [T18](T18-telegraf-doc-consistency.md) | Telegraf drop-in contradicts header; QoS 0 + persistent | P3 | XS | docs |
| [T19](T19-systemd-unit-hardening-compat.md) | systemd: uv under ProtectSystem, duplicate logs, --frozen | P3 | XS | ops |
| [T21](T21-engine-thread-safety.md) | Engine updated from paho thread without lock | P3 | S | latent race |
| [T22](T22-readme-topic-contract.md) | README with MQTT topic contract | P2 | M | docs |
| [T23](T23-alert-webhook-nonblocking.md) | Webhook blocks main loop; per-issue recovery | P3 | S | quality |
| [T24](T24-ignore-retained-charger-messages.md) | Retained charger message treated as fresh | P3 | XS | bug |

Done directly (2026-09-10), no ticket needed:
- Validator overhaul → `energy_hub/validation.py` (bounds / first-reading floor / monotonic / rate; held-for-confirmation for counter jumps and power jumps > 3 kW, so single garbage values never reach the output and a poisoned baseline recovers in two readings; forced re-baseline after N failures as backstop; device time never rejects, clock offset tracked). Superseded T06, T07.
- `energy_hub/timeutil.py` single ISO parser (was T20); ruff import fixes.
- Alignment hardening (was T09): `max_alignment_skew` guard, fallback to current reading instead of oldest history entry, device time only trusted while in sync with host.
- pytest/ruff scaffolding, 30 tests.
- Anker import compatibility for both library layouts (T14 steps 0–2).
- Gas tracker: documented sync key, no phantom impulse on start/retained messages, atomic state file, retained sync cleared, publish on connect and on battery change (T02–T05).
- Grid failover: energy counters stay on the Tasmota meter (`energy_source`/`energy_stale` fields), stale readings used symmetrically, fail-back hysteresis via `sources.tasmota_grid.failback_after`, alert backoff survives recovery, `grid_tasmota_stale` info issue while Anker covers, per-issue recovery messages (T08, T25, T26).

Dependencies: T01 → all. T14 removes the import stub introduced in T01. T16 and T17
change the snapshot/stale semantics, so do them before T22 (README). 

Owner answers (2026-09-10), already folded into the tickets:
1. Host timezone probably Europe/Berlin, Tasmota `Timezone` unknown → T09 keeps the clock-offset diagnostics as required.
2. Deployed `anker-solix-api` is still 3.5.x (`api` package); local checkout is 3.8.2 (`anker_solix_api`) → T14 imports must support both until the box is upgraded.
3. Dead sources: publish `null` to evcc → T16 option A.
