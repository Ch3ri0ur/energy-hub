# T01: Extend the test suite (scaffolding already exists)

**Priority:** P1 (foundation, do first) | **Effort:** M | **Area:** tooling, tests

## Status
Done on 2026-09-10 as part of the validator overhaul: `pyproject.toml` has the `dev`
dependency group, pytest and ruff config; `tests/` contains `test_validator.py`,
`test_engine.py`, `test_timeutil.py` (30 tests, `uv run pytest` green,
`uv run ruff check energy_hub tests` clean). **Remaining work is steps 2–4 below**
(conftest stub, `FakeClient`, the adapter/gas/publisher tests).

## Problem
Every other ticket says "add a test". Also `energy_hub/main.py:17`
imports `anker_plugin`, which imports `api.api` (the `anker-solix-api` package) at
module load, so importing `energy_hub.main` in tests fails on a machine without that
sibling checkout (see T14).

## Where
- `pyproject.toml` (no dev deps, no pytest/ruff config)
- no `tests/` directory

## Fix
1. (done) pytest/ruff config in `pyproject.toml`.
2. Create `tests/conftest.py` that stubs the Anker library so `energy_hub.main` imports:
   ```python
   import sys, types
   api = types.ModuleType("api"); api_api = types.ModuleType("api.api"); api_err = types.ModuleType("api.errors")
   api_api.AnkerSolixApi = object
   class AnkerSolixError(Exception): ...
   api_err.AnkerSolixError = AnkerSolixError
   sys.modules.setdefault("api", api); sys.modules.setdefault("api.api", api_api); sys.modules.setdefault("api.errors", api_err)
   ```
   (Remove the stub once T14 makes the import lazy.)
3. Create `tests/fake_mqtt.py` with a `FakeClient` that records `publish(topic, payload, qos, retain)` calls into a list — used by gas tracker and publisher tests.
4. Add initial tests (one file per module):
   - `tests/test_tasmota_sml.py`: valid payload → `SensorReading(power=1234.5, energy_import=65432.1, energy_export=0.0, timestamp="2026-04-12T14:30:00", source="pv")`; missing `SML` key → `None`; bytes input accepted; garbage → `None`.
   - `tests/test_charger.py`: 16-element list with `[11]=3700` → `3700.0`; list of 5 → `None`; `{}` → `None`.
   - `tests/test_anker_data.py`: payload with only meter keys → `(grid, None)`; only solarbank keys → `(None, sb)`; both → both; `net_grid_power` missing → computed `g2h - p2g`; `meter_updated_at="2026-04-12T12:00:00Z"` → `received_at` equals that epoch.
   - (done) `tests/test_validator.py`, `tests/test_engine.py`.
   - `tests/test_gas_tracker.py`: use `tmp_path` for `state_file`, feed `{"contact": false}` then `{"contact": true}` → `impulse_count == 1` and one publish on `energy/gas`.
   - `tests/test_publisher.py`: `publish_snapshot` publishes 8 topics with the configured prefix, all retained when `retain=True`; `publish_alert` is never retained.
5. Run `uv run pytest` and `uv run ruff check energy_hub tests`; fix anything ruff flags in `F` (unused imports etc.).

## Acceptance criteria
- `uv run pytest` passes (>= 45 tests).
- `uv run ruff check energy_hub tests` passes.
- No production code behaviour change beyond ruff auto-fixes.
