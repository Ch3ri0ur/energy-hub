# T05: Gas tracker — retained sync messages re-apply on every restart; no publish until first impulse

**Priority:** P2 (bug/robustness) | **Effort:** S | **Area:** gas tracker

## Problem
1. If someone publishes the sync message with the retain flag (`mosquitto_pub -r ...`,
   which is common), the broker re-delivers it on every reconnect/restart and
   `_handle_sync` **resets the counter back to that old value**, silently discarding all
   impulses counted since. Nothing clears the retained sync message.
2. After a restart, `energy/gas` is only published when the next impulse arrives.
   Battery info is updated on every Zigbee message (`gas_tracker.py:97-100`) but only
   published on an impulse, so `battery`/`battery_low` on the topic can be hours old.

## Where
- `energy_hub/gas_tracker.py:115-132` — `_handle_sync`
- `energy_hub/gas_tracker.py:85-113` — `_handle_impulse` (battery update without publish)
- `energy_hub/main.py:133-140` — `on_connect` (good place to publish current state)

## Fix
1. In `_handle_sync`, after applying a sync, clear the retained message:
   `self._client.publish(self._cfg.sync_topic, payload=None, qos=0, retain=True)`.
   The hub is itself subscribed to the sync topic and will receive the empty payload:
   in `_handle_sync`, `if not raw: return` silently before JSON parsing.
2. Add a public `publish()` method (rename `_publish` → `publish`, keep a `_publish`
   alias if needed) and call `gas.publish()` from `on_connect` in `main.py` after the
   subscriptions, so the topic is refreshed on every (re)connect.
3. In `_handle_impulse`, if `battery` or `battery_low` changed compared to the stored
   value, call `publish()` even without an impulse.

## Acceptance criteria
- After a sync, a retained-clear publish (`payload=None`/`b""`, `retain=True`) is sent to the sync topic.
- An empty payload on the sync topic produces no warning log.
- A message `{"contact": false, "battery": 90}` after a previous `battery: 100` triggers a publish with `battery: 90`.

## Tests
Extend `tests/test_gas_tracker.py` with the three cases using the `FakeClient`.
