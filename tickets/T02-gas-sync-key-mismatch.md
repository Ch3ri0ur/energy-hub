# T02: Gas sync message key mismatch (`m3` vs documented `volume_m3`)

**Priority:** P1 (bug) | **Effort:** XS | **Area:** gas tracker

## Problem
Documentation says to sync the gas meter by publishing `{"volume_m3": 12345.67}` to
`energy/gas/set`, but the code reads `data["m3"]`. The documented payload is always
rejected with the warning `sync message must be {"m3": <float>}`, so the sync feature
does not work as documented.

## Where
- `energy_hub/gas_tracker.py:118` — `volume_m3 = float(data["m3"])`
- `energy_hub/gas_tracker.py:120` — warning text says `m3`
- Docs that say `volume_m3`: `energy_hub/gas_tracker.py:18`, `config.yaml:71`

## Fix
In `_handle_sync` accept both keys, preferring the documented one:
```python
raw_val = data.get("volume_m3", data.get("m3"))
if raw_val is None:
    raise KeyError("volume_m3")
volume_m3 = float(raw_val)
```
Update the warning text to `sync message must be {"volume_m3": <float>}`.
Also reject negative values (`volume_m3 < 0`) with a warning.

## Acceptance criteria
- Publishing `{"volume_m3": 100.5}` sets `impulse_count` to `10050` (with `impulse_m3=0.01`) and publishes `energy/gas` with `volume_m3: 100.5`.
- `{"m3": 100.5}` still works (backward compatible).
- `{"volume_m3": -1}` is rejected and the counter is unchanged.

## Tests
Add to `tests/test_gas_tracker.py`: three cases above using the `FakeClient` from T01.
