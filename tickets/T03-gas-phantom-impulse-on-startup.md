# T03: Gas tracker counts a phantom impulse on startup / retained message

**Priority:** P1 (bug, corrupts the meter count) | **Effort:** XS | **Area:** gas tracker

## Problem
`_contact_prev` starts as `None`. The rising-edge check is
`contact is True and self._contact_prev is not True`, so the very first message after
a (re)start that has `contact: true` is counted as an impulse. Zigbee2MQTT typically
publishes device state retained, and the reed contact rests in the closed (`true`)
position for most of a drum revolution, so **every restart of the hub while the contact
is closed adds one fake impulse** (0.01 m³). Over many restarts the counter drifts up.

## Where
- `energy_hub/gas_tracker.py:55` — `self._contact_prev: bool | None = None`
- `energy_hub/gas_tracker.py:103` — edge detection condition
- `energy_hub/gas_tracker.py:75-79` — `on_message` (has access to `msg.retain`)

## Fix
1. In `_handle_impulse`, if `self._contact_prev is None`: store the value, update battery
   fields, log at DEBUG "initial contact state = ...", and **return without counting**.
2. Pass `msg.retain` into `_handle_impulse(raw, retained: bool)`; when `retained` is
   true, treat the message as state initialisation only (same as step 1, but always
   overwrite `_contact_prev`), never count.
3. Keep the existing rising-edge logic for subsequent live messages.

## Acceptance criteria
- First message `{"contact": true}` after construction does not increment the counter.
- Sequence `true(retained)`, `false`, `true` → exactly 1 impulse.
- Sequence `false`, `true`, `true`, `false`, `true` → exactly 2 impulses.

## Tests
Add the three sequences above to `tests/test_gas_tracker.py`. Build a minimal
`MQTTMessage`-like object with `.topic`, `.payload`, `.retain`.
