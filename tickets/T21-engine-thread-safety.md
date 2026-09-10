# T21: Engine state is mutated from the paho thread and read from the main thread without a lock

**Priority:** P3 (latent race) | **Effort:** S | **Area:** engine

## Problem
`Engine.update_*` run on paho's network thread (via the message callbacks);
`Engine.snapshot()` and `source_statuses()` run on the main loop thread. There is no
synchronisation. Most single-attribute assignments are atomic under the GIL, but:
- `update_charger` writes `charger_power` and `charger_received_at` separately
  (`engine.py:108-112`) — a snapshot in between sees a new power with an old timestamp.
- `snapshot()` reads `self.grid_tasmota`, then the history deque, then `self.pv_roof`
  … over several hundred lines; a reading can change mid-snapshot, making the
  alignment and staleness computations inconsistent.
The `SensorValidator` dicts/lists are only touched from the paho thread, so they are fine.

## Where
- `energy_hub/engine.py:72-112` — update methods
- `energy_hub/engine.py:236-390` — `snapshot`
- `energy_hub/engine.py:394-421` — `source_statuses`

## Fix
Add `self._lock = threading.RLock()` in `Engine.__init__`; wrap the body of each
`update_*` method, `snapshot()`, and `source_statuses()` in `with self._lock:`.
`snapshot()` holds the lock for well under a millisecond, so message handling is not
noticeably delayed. Keep `active_grid()` callable only from within the lock (it is
only called by `snapshot`).

## Acceptance criteria
- Behaviour unchanged; `uv run pytest` passes.
- A quick stress test (thread calling `update_charger` in a loop while main calls `snapshot()` 10k times) raises no exception and never yields `charger.raw_power != 0` with `stale: true`.
