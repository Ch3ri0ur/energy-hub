# T25: Stale Anker grid data is never used, while stale Tasmota data is (asymmetric fallback)

**Priority:** P3 | **Effort:** XS | **Area:** engine

## Problem
`active_grid()` order is: fresh Tasmota → fresh Anker → **stale Tasmota** → none.
If Tasmota has never delivered a reading since start (`grid_tasmota is None`) and the
Anker meter reading is between `stale_timeout` and the grace deadline, the function
returns `(None, "")` and the snapshot publishes `grid.power = 0, source = ""`, even
though a still-usable (within grace) Anker reading exists. Tasmota in the same
situation is used and flagged `stale`.

## Where
- `energy_hub/engine.py:137-161` — `active_grid`

## Fix
After the "Tasmota stale but still the best we have" branch, add:
```python
if self.grid_anker:
    self._active_grid_source = "anker"
    return self.grid_anker.to_sensor_reading(), "anker"
```
Prefer whichever stale reading is **newer** when both exist (compare `received_at`).
The existing grace/zeroing logic in `snapshot()` already handles the age.

## Acceptance criteria
- `grid_tasmota is None`, Anker reading aged 200 s (stale 300 → within grace): `snapshot()["grid"]["source"] == "anker"`, `stale is True`, `power` equals the Anker net power.
- Both stale, Anker newer → `source == "anker"`; Tasmota newer → `"tasmota"`.

## Tests
`tests/test_engine.py`: both cases.
