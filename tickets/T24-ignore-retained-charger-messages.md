# T24: Retained charger messages are treated as fresh after a restart

**Priority:** P3 | **Effort:** XS | **Area:** main / charger adapter

## Problem
`update_charger` stamps the reading with `time.time()` at receive time. If the
go-eCharger `nrg` topic is retained on the broker (go-e publishes retained by default
in API v2), a restart of the hub receives the last retained `nrg` immediately and treats
it as live for `charger.stale_timeout` (300 s) — e.g. showing 11 kW charging that ended
hours ago, and subtracting it from `house.power`.
Tasmota SML readings are protected by the device timestamp check in the validator, and
the Anker snapshot carries its own timestamps, so only the charger route is affected
(gas is handled in T03).

## Where
- `energy_hub/main.py:80-83` — `on_charger`
- `energy_hub/engine.py:108-112` — `update_charger`

## Fix
In `on_charger`: `if msg.retain: logger.debug("Ignoring retained charger message"); return`.
(paho sets `msg.retain` to 1 for retained deliveries on subscribe.)

## Acceptance criteria
- A message object with `retain=True` does not call `engine.update_charger`.
- A live message (`retain=False`) does.

## Tests
`tests/test_main_router.py`: build the router with a fake engine that records calls.
